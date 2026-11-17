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
        self._wd_to_path: Dict[int, Path] = {}
        self._path_to_wd: Dict[Path, int] = {}
        self._file_wds = set()
        self._watched_inodes: Dict[Path, Tuple[int, int]] = {}
        self._buffer = b''
        self._move_cookies: Dict[int, Tuple[Path, float, bool, asyncio.TimerHandle]] = {}
        self._ignored_wds_pending = set()
        self._synthesized_creates: Dict[Path, float] = {}
        self._synthesized_prune_handle = None
        self._rebuild_scheduled = False

    @property
    def is_running(self) -> bool:
        return self._fd >= 0

    def is_healthy_for(self, paths: List[Path]) -> bool:
        """Check both the inotify instance and coverage of requested roots.

        An inotify fd remains valid after a watched root is removed, while the
        kernel drops that root's watch and sends ``IN_IGNORED``. Treating the
        fd alone as healthy leaves a rapidly recreated root permanently
        unwatched.
        """
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

    def start(self):
        if self._fd >= 0:
            raise RuntimeError("Linux watcher is already running")
        # 1. Initialize inotify instance
        self._fd = inotify_init()
        if self._fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"inotify_init failed: {os.strerror(err)}")

        # Set to non-blocking
        flags = fcntl.fcntl(self._fd, fcntl.F_GETFL)
        fcntl.fcntl(self._fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        
        # Set close-on-exec
        fd_flags = fcntl.fcntl(self._fd, fcntl.F_GETFD)
        fcntl.fcntl(self._fd, fcntl.F_SETFD, fd_flags | fcntl.FD_CLOEXEC)

        # 2. Register watches for all paths
        try:
            for p in self.paths:
                if p.exists():
                    self._register_path(p)
        except Exception as e:
            self.stop()
            raise e

        # 3. Add to asyncio loop readers
        self.loop.add_reader(self._fd, self._handle_raw_read)

    def stop(self):
        if self._fd >= 0:
            try:
                self.loop.remove_reader(self._fd)
            except Exception:
                pass
            
            # Remove all watches
            for wd in list(self._wd_to_path.keys()):
                try:
                    inotify_rm_watch(self._fd, wd)
                except Exception:
                    pass
            
            try:
                os.close(self._fd)
            except Exception:
                pass
            
            self._fd = -1
        self._wd_to_path.clear()
        self._path_to_wd.clear()
        self._file_wds.clear()
        self._watched_inodes.clear()
        self._buffer = b''
        for _, _, _, timer in self._move_cookies.values():
            timer.cancel()
        self._move_cookies.clear()
        self._ignored_wds_pending.clear()
        if self._synthesized_prune_handle is not None:
            self._synthesized_prune_handle.cancel()
            self._synthesized_prune_handle = None
        self._synthesized_creates.clear()
        self._rebuild_scheduled = False

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

    def _register_path(self, path: Path, emit_existing: bool = False):
        path = path.resolve()
        if path in self._path_to_wd:
            return
        if not path.is_dir():
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

        if not self._should_recurse_into(path):
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
        stack = [path]
        while stack:
            dir_path = stack.pop()
            if self._ignore_filter and self._ignore_filter.is_ignored(dir_path, is_dir=True):
                continue

            if dir_path not in self._path_to_wd:
                wd = inotify_add_watch(self._fd, os.fsencode(dir_path), InotifyMask.WATCH_MASK)
                if wd < 0:
                    err = ctypes.get_errno()
                    if err in (errno.ENOSPC, errno.EMFILE) and not emit_existing:
                        raise OSError(err, f"inotify watch limit exceeded: {os.strerror(err)}")
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
                        try:
                            entry_is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        entry_path = dir_path / entry.name
                        if self._ignore_filter and self._ignore_filter.is_ignored(entry_path, is_dir=entry_is_dir):
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

    def _handle_raw_read(self):
        while True:
            try:
                data = os.read(self._fd, 65536)
                if not data:
                    break
                self._buffer += data
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

        while offset + header_size <= len(self._buffer):
            wd, mask, cookie, name_len = struct.unpack_from('iIII', self._buffer, offset)
            total_len = header_size + name_len

            if offset + total_len > len(self._buffer):
                break  # Incomplete event, wait for more data

            # Extract name and strip padding nulls
            name_bytes = self._buffer[offset + header_size : offset + total_len]
            name = os.fsdecode(name_bytes.rstrip(b'\x00'))

            self._dispatch_event(wd, mask, cookie, name)
            offset += total_len

        self._buffer = self._buffer[offset:]

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
        replacements = []
        for watched_path, wd in list(self._path_to_wd.items()):
            try:
                relative = watched_path.relative_to(old_path)
            except ValueError:
                continue
            replacements.append((watched_path, new_path / relative, wd))

        for watched_path, new_watched_path, wd in replacements:
            self._path_to_wd.pop(watched_path, None)
            self._path_to_wd[new_watched_path] = wd
            self._wd_to_path[wd] = new_watched_path

    def _remove_watch_subtree(self, path: Path):
        watches = []
        for watched_path, wd in list(self._path_to_wd.items()):
            try:
                watched_path.relative_to(path)
            except ValueError:
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

    def _rebuild_watches(self):
        """Reconcile recursive watches after the kernel queue overflowed."""
        self._rebuild_scheduled = False
        if self._fd < 0:
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
