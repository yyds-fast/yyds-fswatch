# -*- coding: utf-8 -*-

import ctypes
import ctypes.util
import os
import struct
import fcntl
import errno
import time
import asyncio
from pathlib import Path
from typing import List, Callable, Dict, Tuple

from ..ignore import path_prefix_pair, subtree_relative_suffix
from ._base import BaseWatcher

# Load libc
try:
    libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
except Exception as e:
    raise ImportError(f"Failed to load libc: {e}")

# Bind inotify C APIs
if not hasattr(libc, "inotify_init") or not hasattr(libc, "inotify_add_watch") or not hasattr(libc, "inotify_rm_watch"):
    raise ImportError("libc does not support inotify")

inotify_init = libc.inotify_init
inotify_init.argtypes = []
inotify_init.restype = ctypes.c_int

inotify_init1 = getattr(libc, "inotify_init1", None)
if inotify_init1 is not None:
    inotify_init1.argtypes = [ctypes.c_int]
    inotify_init1.restype = ctypes.c_int

inotify_add_watch = libc.inotify_add_watch
inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
inotify_add_watch.restype = ctypes.c_int

inotify_rm_watch = libc.inotify_rm_watch
inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
inotify_rm_watch.restype = ctypes.c_int

# Inotify Constants
class InotifyMask:
    IN_ACCESS        = 0x00000001
    IN_MODIFY        = 0x00000002
    IN_ATTRIB        = 0x00000004
    IN_CLOSE_WRITE   = 0x00000008
    IN_CLOSE_NOWRITE = 0x00000010
    IN_OPEN          = 0x00000020
    IN_MOVED_FROM    = 0x00000040
    IN_MOVED_TO      = 0x00000080
    IN_CREATE        = 0x00000100
    IN_DELETE        = 0x00000200
    IN_DELETE_SELF   = 0x00000400
    IN_MOVE_SELF     = 0x00000800
    IN_UNMOUNT       = 0x00002000
    IN_Q_OVERFLOW    = 0x00004000
    IN_IGNORED       = 0x00008000
    IN_ISDIR         = 0x40000000

    WATCH_MASK = (IN_CREATE | IN_DELETE | IN_MODIFY | IN_MOVED_FROM |
                  IN_MOVED_TO | IN_CLOSE_WRITE | IN_ATTRIB |
                  IN_DELETE_SELF | IN_MOVE_SELF | IN_UNMOUNT)

class LinuxWatcher(BaseWatcher):
    def __init__(self, paths: List[Path], recursive: bool, loop: asyncio.AbstractEventLoop, 
                 on_raw_event: Callable, ignore_filter=None, recursive_roots=None):
        super().__init__(
            paths,
            recursive,
            loop,
            on_raw_event,
            ignore_filter=ignore_filter,
            recursive_roots=recursive_roots,
        )
        self._fd = -1
        self._starting = False
        self._stopping = False
        self._wd_to_path: Dict[int, Path] = {}
        self._path_to_wd: Dict[Path, int] = {}
        self._file_wds = set()
        self._watched_inodes: Dict[Path, Tuple[int, int]] = {}
        self._buffer = bytearray()
        self._move_cookies: Dict[int, Tuple[Path, float, bool, asyncio.TimerHandle]] = {}
        self._ignored_wds_pending = set()
        self._synthesized_creates: Dict[Path, float] = {}
        self._synthesized_prune_handle = None
        self._rebuild_scheduled = False
        self._reconcile_scheduled = False

    @property
    def is_running(self) -> bool:
        return self._fd >= 0 and not self._stopping

    def is_healthy_for(self, paths: List[Path]) -> bool:
        """Check both the inotify instance and coverage of requested roots.

        An inotify fd remains valid after a watched root is removed, while the
        kernel drops that root's watch and sends ``IN_IGNORED``. Treating the
        fd alone as healthy leaves a rapidly recreated root permanently
        unwatched.
        """
        if self._starting and not self._stopping:
            return True
        if not self.is_running:
            return False
        for path in paths:
            resolved = path.resolve()
            try:
                st = os.stat(resolved)
            except OSError:
                continue
            if resolved not in self._path_to_wd:
                return False
            expected_inode = self._watched_inodes.get(resolved)
            if expected_inode is not None and (st.st_dev, st.st_ino) != expected_inode:
                return False
        return True

    def _open_inotify_fd(self) -> int:
        cloexec_flag = getattr(os, "O_CLOEXEC", 0x80000)
        if inotify_init1 is not None:
            fd = inotify_init1(os.O_NONBLOCK | cloexec_flag)
            if fd >= 0:
                return fd
            err = ctypes.get_errno()
            if err not in (errno.ENOSYS, errno.EINVAL):
                raise OSError(err, f"inotify_init1 failed: {os.strerror(err)}")

        fd = inotify_init()
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"inotify_init failed: {os.strerror(err)}")
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        fd_flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, fd_flags | fcntl.FD_CLOEXEC)
        return fd

    def _init_and_register(self):
        if self._fd >= 0:
            raise RuntimeError("Linux watcher is already running")
        self._stopping = False
        self._fd = self._open_inotify_fd()
        try:
            for p in self.paths:
                if self._stopping:
                    break
                if p.exists():
                    self._register_path(p)
        except Exception:
            self.stop()
            raise

    def start(self):
        self._init_and_register()
        if self._fd >= 0 and not self._stopping:
            self.loop.add_reader(self._fd, self._handle_raw_read)

    async def astart(self):
        if self.recursive and self._recursive_roots:
            self._starting = True
            try:
                await self._run_in_helper_thread(
                    self._init_and_register,
                    "yyds-fswatch-inotify-init",
                )
            except BaseException:
                self.stop()
                raise
            finally:
                self._starting = False
            if self._fd >= 0 and not self._stopping:
                self.loop.add_reader(self._fd, self._handle_raw_read)
        else:
            self.start()

    def stop(self):
        self._stopping = True
        if self._fd >= 0:
            fd = self._fd
            self._fd = -1
            try:
                self.loop.remove_reader(fd)
            except Exception:
                pass
            
            # Remove all watches
            for wd in list(self._wd_to_path.keys()):
                try:
                    inotify_rm_watch(fd, wd)
                except Exception:
                    pass
            
            try:
                os.close(fd)
            except Exception:
                pass
        self._wd_to_path.clear()
        self._path_to_wd.clear()
        self._file_wds.clear()
        self._watched_inodes.clear()
        self._buffer.clear()
        for _, _, _, timer in self._move_cookies.values():
            timer.cancel()
        self._move_cookies.clear()
        self._ignored_wds_pending.clear()
        if self._synthesized_prune_handle is not None:
            self._synthesized_prune_handle.cancel()
            self._synthesized_prune_handle = None
        self._synthesized_creates.clear()
        self._rebuild_scheduled = False
        self._reconcile_scheduled = False

    def _record_root_inode(self, path: Path):
        if path in self._watched_roots:
            try:
                st = os.stat(path)
                self._watched_inodes[path] = (st.st_dev, st.st_ino)
            except OSError:
                pass

    def _record_synthesized_create(self, path: Path):
        now = time.monotonic()
        self._synthesized_creates[path] = now
        if self._synthesized_prune_handle is None and self.loop and not self.loop.is_closed():
            self._synthesized_prune_handle = self.loop.call_later(
                0.5, self._prune_synthesized_creates
            )

    def _consume_synthesized_create(self, path: Path) -> bool:
        ts = self._synthesized_creates.pop(path, None)
        if ts is None:
            return False
        return (time.monotonic() - ts) < 0.5

    def _prune_synthesized_creates(self):
        self._synthesized_prune_handle = None
        now = time.monotonic()
        expired = [p for p, ts in self._synthesized_creates.items() if now - ts >= 0.5]
        for p in expired:
            self._synthesized_creates.pop(p, None)
        if self._synthesized_creates and self._fd >= 0 and self.loop and not self.loop.is_closed():
            self._synthesized_prune_handle = self.loop.call_later(
                0.5, self._prune_synthesized_creates
            )

    def _is_path_ignored(self, path: Path, is_dir: bool) -> bool:
        filt = self._ignore_filter
        if filt is None:
            return False
        ignore_fn = getattr(filt, "is_ignored_canonical", None) or getattr(filt, "is_ignored", None)
        if ignore_fn is None:
            return False
        return bool(ignore_fn(path, is_dir))

    def _register_path(self, path: Path, emit_existing: bool = False,
                       force_rescan: bool = False):
        if self._stopping or self._fd < 0:
            return
        path = path.resolve()
        if path in self._path_to_wd and not force_rescan:
            return
        if not path.is_dir():
            if path in self._path_to_wd:
                return
            # Watch single file
            wd = inotify_add_watch(self._fd, os.fsencode(path), InotifyMask.WATCH_MASK)
            if wd < 0:
                err = ctypes.get_errno()
                raise OSError(err, f"inotify_add_watch failed for {path}: {os.strerror(err)}")
            self._wd_to_path[wd] = path
            self._path_to_wd[path] = wd
            self._file_wds.add(wd)
            self._record_root_inode(path)
            return

        if not self._should_recurse_into(path, resolved=True):
            if path in self._path_to_wd:
                return
            # Single directory
            wd = inotify_add_watch(self._fd, os.fsencode(path), InotifyMask.WATCH_MASK)
            if wd < 0:
                err = ctypes.get_errno()
                raise OSError(err, f"inotify_add_watch failed for {path}: {os.strerror(err)}")
            self._wd_to_path[wd] = path
            self._path_to_wd[path] = wd
            self._record_root_inode(path)
            return

        # Recursive registration: add inotify watch on each directory BEFORE
        # scanning its children so no created child falls into the race gap.
        limit_reported = False
        stack = [path]
        while stack:
            if self._stopping or self._fd < 0:
                return
            dir_path = stack.pop()
            if self._is_path_ignored(dir_path, is_dir=True):
                continue

            if dir_path not in self._path_to_wd:
                wd = inotify_add_watch(self._fd, os.fsencode(dir_path), InotifyMask.WATCH_MASK)
                if wd < 0:
                    err = ctypes.get_errno()
                    if err in (errno.ENOSPC, errno.EMFILE):
                        exc = OSError(err, f"inotify watch limit exceeded: {os.strerror(err)}")
                        if not emit_existing and not force_rescan:
                            raise exc
                        if not limit_reported:
                            limit_reported = True
                            self.on_raw_event("overflow", dir_path, is_dir=False)
                            if self.loop and not self.loop.is_closed():
                                self.loop.call_exception_handler({
                                    "message": "yyds-fswatch inotify watch limit exceeded at runtime",
                                    "exception": exc,
                                })
                    continue
                self._wd_to_path[wd] = dir_path
                self._path_to_wd[dir_path] = wd
                self._record_root_inode(dir_path)
                if emit_existing and dir_path != path:
                    self._record_synthesized_create(dir_path)
                    self.on_raw_event("created", dir_path, True)

            try:
                with os.scandir(dir_path) as entries:
                    subdirs = []
                    for entry in entries:
                        if self._stopping or self._fd < 0:
                            return
                        try:
                            entry_is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        entry_path = dir_path / entry.name
                        if self._is_path_ignored(entry_path, is_dir=entry_is_dir):
                            continue
                        if entry_is_dir:
                            subdirs.append(entry_path)
                        elif emit_existing:
                            self._record_synthesized_create(entry_path)
                            self.on_raw_event("created", entry_path, False)
                    for subdir in reversed(subdirs):
                        stack.append(subdir)
            except OSError:
                continue

    def reconcile_watches(self):
        """Schedule watch reconciliation after ignore rules change."""
        if not self.is_running or not self.recursive or not self._recursive_roots:
            return
        if self.loop is not None and self.loop.is_running():
            if not self._reconcile_scheduled:
                self._reconcile_scheduled = True
                self.loop.call_soon(self._reconcile_watches_now)
            return
        self._reconcile_watches_now()

    def _reconcile_watches_now(self):
        self._reconcile_scheduled = False
        if not self.is_running or not self.recursive or not self._recursive_roots:
            return
        if self._ignore_filter is not None:
            for watched_path in list(self._path_to_wd):
                if watched_path in self._watched_roots:
                    continue
                if watched_path not in self._path_to_wd:
                    continue
                if self._is_path_ignored(watched_path, is_dir=True):
                    self._remove_watch_subtree(watched_path)

        for p in self.paths:
            if self._stopping or self._fd < 0:
                break
            if p.exists() and self._should_recurse_into(p):
                try:
                    self._register_path(p, emit_existing=False, force_rescan=True)
                except Exception:
                    pass

    def _handle_raw_read(self):
        # Bound per-callback reads to 1 MiB so an uninterrupted kernel event
        # storm cannot starve other coroutines or prevent stop() from running.
        for _ in range(16):
            if self._fd < 0:
                return
            try:
                data = os.read(self._fd, 65536)
                if not data:
                    break
                self._buffer.extend(data)
            except BlockingIOError:
                break
            except OSError as exc:
                if self._fd >= 0:
                    self.stop()
                    self.loop.call_exception_handler({
                        "message": "yyds-fswatch inotify reader failed",
                        "exception": exc,
                    })
                return

        offset = 0
        header_size = 16  # wd(4) + mask(4) + cookie(4) + len(4)
        buf_len = len(self._buffer)

        while offset + header_size <= buf_len:
            if self._fd < 0:
                return
            wd, mask, cookie, name_len = struct.unpack_from('iIII', self._buffer, offset)
            total_len = header_size + name_len

            if offset + total_len > buf_len:
                break  # Incomplete event, wait for more data

            # Extract name and strip padding nulls
            name_bytes = bytes(self._buffer[offset + header_size : offset + total_len])
            name = os.fsdecode(name_bytes.rstrip(b'\x00'))

            self._dispatch_event(wd, mask, cookie, name)
            offset += total_len

        if offset and self._fd >= 0:
            del self._buffer[:offset]

    def _dispatch_event(self, wd: int, mask: int, cookie: int, name: str):
        if mask & InotifyMask.IN_Q_OVERFLOW:
            root_path = self.paths[0] if self.paths else Path('.')
            self.on_raw_event("overflow", root_path, is_dir=False)
            if not self._rebuild_scheduled:
                self._rebuild_scheduled = True
                self.loop.call_soon(self._rebuild_watches)
            return

        # IN_IGNORED may arrive after we deliberately removed and unmapped a
        # watch, so it must be handled before looking up the path.
        if mask & InotifyMask.IN_IGNORED:
            self._file_wds.discard(wd)
            if wd in self._ignored_wds_pending:
                self._ignored_wds_pending.discard(wd)
                return
            dir_path = self._wd_to_path.pop(wd, None)
            if dir_path is not None and self._path_to_wd.get(dir_path) == wd:
                self._path_to_wd.pop(dir_path, None)
                self._watched_inodes.pop(dir_path, None)
            return

        dir_path = self._wd_to_path.get(wd)
        if not dir_path:
            return

        event_path = dir_path / name if name else dir_path
        is_dir = bool(mask & InotifyMask.IN_ISDIR)

        # Handle DELETE_SELF / MOVE_SELF / UNMOUNT after resolving the watched path.
        if mask & InotifyMask.IN_UNMOUNT:
            is_self_dir = wd not in self._file_wds
            self._remove_watch_subtree(dir_path)
            self.on_raw_event("deleted", dir_path, is_dir=is_self_dir)
            return

        if mask & InotifyMask.IN_DELETE_SELF:
            if dir_path in self._watched_roots or dir_path.parent not in self._path_to_wd:
                self.on_raw_event("deleted", dir_path, is_dir=(wd not in self._file_wds))
            return

        if mask & InotifyMask.IN_MOVE_SELF:
            if dir_path in self._watched_roots and dir_path.parent not in self._path_to_wd:
                is_self_dir = wd not in self._file_wds
                self._remove_watch_subtree(dir_path)
                self.on_raw_event("deleted", dir_path, is_dir=is_self_dir)
            return

        # Handle standard events
        if mask & InotifyMask.IN_CREATE:
            if self._consume_synthesized_create(event_path):
                if is_dir and self._should_recurse_into(event_path) and event_path not in self._path_to_wd:
                    try:
                        self._register_path(event_path, emit_existing=True)
                    except Exception:
                        pass
                return
            self.on_raw_event("created", event_path, is_dir)
            if is_dir and self._should_recurse_into(event_path):
                # Dynamically watch new directories (and scan children created
                # before inotify_add_watch attached to the new directory).
                try:
                    self._register_path(event_path, emit_existing=True)
                except Exception:
                    pass

        elif mask & InotifyMask.IN_DELETE:
            self._synthesized_creates.pop(event_path, None)
            self.on_raw_event("deleted", event_path, is_dir)

        elif mask & (InotifyMask.IN_MODIFY | InotifyMask.IN_CLOSE_WRITE | InotifyMask.IN_ATTRIB):
            self.on_raw_event("modified", event_path, is_dir)

        elif mask & InotifyMask.IN_MOVED_FROM:
            self._synthesized_creates.pop(event_path, None)
            previous = self._move_cookies.pop(cookie, None)
            if previous is not None:
                previous[3].cancel()
            # Store rename cookie
            timer = self.loop.call_later(0.05, self._expire_cookie, cookie)
            self._move_cookies[cookie] = (event_path, time.monotonic(), is_dir, timer)

        elif mask & InotifyMask.IN_MOVED_TO:
            if cookie in self._move_cookies:
                old_path, _, _, timer = self._move_cookies.pop(cookie)
                timer.cancel()
                self.on_raw_event("moved", old_path, is_dir, dest_path=event_path)
                
                # Update watch mappings recursively for directory renames
                if is_dir:
                    destination_ignored = bool(
                        self._ignore_filter and
                        self._ignore_filter.is_ignored(event_path, is_dir=True)
                    )
                    if destination_ignored:
                        self._remove_watch_subtree(old_path)
                    else:
                        was_watched = old_path in self._path_to_wd
                        self._rename_watch_subtree(old_path, event_path)
                        if not was_watched and self._should_recurse_into(event_path):
                            try:
                                self._register_path(event_path, emit_existing=True)
                            except Exception:
                                pass
            else:
                # Treat as created if no matching cookie
                if self._consume_synthesized_create(event_path):
                    if is_dir and self._should_recurse_into(event_path) and event_path not in self._path_to_wd:
                        try:
                            self._register_path(event_path, emit_existing=True)
                        except Exception:
                            pass
                    return
                self.on_raw_event("created", event_path, is_dir)
                if is_dir and self._should_recurse_into(event_path):
                    try:
                        self._register_path(event_path, emit_existing=True)
                    except Exception:
                        pass

    def _expire_cookie(self, cookie: int):
        if cookie in self._move_cookies:
            old_path, _, is_dir, _ = self._move_cookies.pop(cookie)
            # Cookie expired -> fallback to deleted event
            self.on_raw_event("deleted", old_path, is_dir)
            if is_dir:
                # inotify watches follow the inode when a directory is moved
                # outside the root. Remove them or future events would be
                # reported under stale paths outside the requested scope.
                self._remove_watch_subtree(old_path)

    def _rename_watch_subtree(self, old_path: Path, new_path: Path):
        exact_norm, prefix_norm = path_prefix_pair(old_path)
        replacements = []
        for watched_path, wd in list(self._path_to_wd.items()):
            suffix = subtree_relative_suffix(watched_path, exact_norm, prefix_norm)
            if suffix is None:
                continue
            dest = new_path / suffix if suffix else new_path
            replacements.append((watched_path, dest, wd))

        for watched_path, new_watched_path, wd in replacements:
            self._path_to_wd.pop(watched_path, None)
            prev_wd = self._path_to_wd.get(new_watched_path)
            if prev_wd is not None and prev_wd != wd:
                self._wd_to_path.pop(prev_wd, None)
                self._file_wds.discard(prev_wd)
            self._path_to_wd[new_watched_path] = wd
            self._wd_to_path[wd] = new_watched_path

    def _remove_watch_subtree(self, path: Path):
        exact_norm, prefix_norm = path_prefix_pair(path)
        watches = []
        for watched_path, wd in list(self._path_to_wd.items()):
            if subtree_relative_suffix(watched_path, exact_norm, prefix_norm) is None:
                continue
            watches.append((watched_path, wd))

        for watched_path, wd in watches:
            self._path_to_wd.pop(watched_path, None)
            self._wd_to_path.pop(wd, None)
            self._file_wds.discard(wd)
            self._watched_inodes.pop(watched_path, None)
            if self._fd >= 0:
                self._ignored_wds_pending.add(wd)
                inotify_rm_watch(self._fd, wd)

    async def _arebuild_watches(self):
        try:
            self.stop()
            await self.astart()
        except Exception as exc:
            self.loop.call_exception_handler({
                "message": "yyds-fswatch failed to rebuild inotify watches after overflow",
                "exception": exc,
            })

    def _rebuild_watches(self):
        """Reconcile recursive watches after the kernel queue overflowed."""
        self._rebuild_scheduled = False
        self._reconcile_scheduled = False
        if self._fd < 0 or self._stopping:
            return
        if self.loop and self.loop.is_running():
            self.loop.create_task(self._arebuild_watches())
            return
        try:
            # Recreate the inotify instance rather than reusing watch
            # descriptors. Otherwise queued IN_IGNORED records for removed
            # descriptors can race with descriptor-number reuse.
            self.stop()
            self.start()
        except Exception as exc:
            self.loop.call_exception_handler({
                "message": "yyds-fswatch failed to rebuild inotify watches after overflow",
                "exception": exc,
            })
