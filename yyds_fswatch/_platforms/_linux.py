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
                 on_raw_event: Callable, ignore_filter=None):
        super().__init__(paths, recursive, loop, on_raw_event)
        self._ignore_filter = ignore_filter
        self._fd = -1
        self._wd_to_path: Dict[int, Path] = {}
        self._path_to_wd: Dict[Path, int] = {}
        self._buffer = b''
        self._move_cookies: Dict[int, Tuple[Path, float, bool]] = {}

    def start(self):
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
        self._buffer = b''
        self._move_cookies.clear()

    def _register_path(self, path: Path):
        path = path.resolve()
        if not path.is_dir():
            # Watch single file
            wd = inotify_add_watch(self._fd, str(path).encode('utf-8'), InotifyMask.WATCH_MASK)
            if wd < 0:
                err = ctypes.get_errno()
                raise OSError(err, f"inotify_add_watch failed for {path}: {os.strerror(err)}")
            self._wd_to_path[wd] = path
            self._path_to_wd[path] = wd
            return

        if not self.recursive:
            # Single directory
            wd = inotify_add_watch(self._fd, str(path).encode('utf-8'), InotifyMask.WATCH_MASK)
            if wd < 0:
                err = ctypes.get_errno()
                raise OSError(err, f"inotify_add_watch failed for {path}: {os.strerror(err)}")
            self._wd_to_path[wd] = path
            self._path_to_wd[path] = wd
            return

        # Recursive registration
        for dirpath, dirnames, filenames in os.walk(path):
            dir_path = Path(dirpath)
            
            # Prune directory tree walk using .gitignore filter
            if self._ignore_filter and self._ignore_filter.is_ignored(dir_path, is_dir=True):
                dirnames[:] = []  # Stop descending
                continue

            if self._ignore_filter:
                dirnames[:] = [
                    d for d in dirnames 
                    if not self._ignore_filter.is_ignored(dir_path / d, is_dir=True)
                ]

            wd = inotify_add_watch(self._fd, str(dir_path).encode('utf-8'), InotifyMask.WATCH_MASK)
            if wd < 0:
                err = ctypes.get_errno()
                if err in (errno.ENOSPC, errno.EMFILE):
                    raise OSError(err, f"inotify watch limit exceeded: {os.strerror(err)}")
                continue # Skip directories with permission issues or other errors
                
            self._wd_to_path[wd] = dir_path
            self._path_to_wd[dir_path] = wd

    def _handle_raw_read(self):
        while True:
            try:
                data = os.read(self._fd, 65536)
                if not data:
                    break
                self._buffer += data
            except BlockingIOError:
                break
            except OSError:
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
            name = name_bytes.rstrip(b'\x00').decode('utf-8', errors='replace')

            self._dispatch_event(wd, mask, cookie, name)
            offset += total_len

        self._buffer = self._buffer[offset:]

    def _dispatch_event(self, wd: int, mask: int, cookie: int, name: str):
        dir_path = self._wd_to_path.get(wd)
        if not dir_path:
            return

        event_path = dir_path / name if name else dir_path
        is_dir = bool(mask & InotifyMask.IN_ISDIR)

        # Handle IGNORED & DELETE_SELF
        if mask & InotifyMask.IN_IGNORED:
            # Clean up watch mappings
            if dir_path in self._path_to_wd:
                self._wd_to_path.pop(wd, None)
                self._path_to_wd.pop(dir_path, None)
            return

        if mask & InotifyMask.IN_DELETE_SELF:
            self.on_raw_event("deleted", dir_path, is_dir=True)
            return

        if mask & InotifyMask.IN_UNMOUNT:
            self.on_raw_event("deleted", dir_path, is_dir=True)
            return

        # Handle standard events
        if mask & InotifyMask.IN_CREATE:
            if is_dir and self.recursive:
                # Dynamically watch new directories
                try:
                    self._register_path(event_path)
                except Exception:
                    pass
            self.on_raw_event("created", event_path, is_dir)

        elif mask & InotifyMask.IN_DELETE:
            self.on_raw_event("deleted", event_path, is_dir)

        elif mask & (InotifyMask.IN_MODIFY | InotifyMask.IN_CLOSE_WRITE | InotifyMask.IN_ATTRIB):
            self.on_raw_event("modified", event_path, is_dir)

        elif mask & InotifyMask.IN_MOVED_FROM:
            # Store rename cookie
            self._move_cookies[cookie] = (event_path, time.monotonic(), is_dir)
            self.loop.call_later(0.05, self._expire_cookie, cookie)

        elif mask & InotifyMask.IN_MOVED_TO:
            if cookie in self._move_cookies:
                old_path, _, _ = self._move_cookies.pop(cookie)
                self.on_raw_event("moved", old_path, is_dir, dest_path=event_path)
                
                # Update watch mappings recursively for directory renames
                if is_dir:
                    if old_path in self._path_to_wd:
                        wd = self._path_to_wd.pop(old_path)
                        self._path_to_wd[event_path] = wd
                        self._wd_to_path[wd] = event_path
                        
                    if self.recursive:
                        for path in list(self._path_to_wd.keys()):
                            try:
                                rel_path = path.relative_to(old_path)
                                new_sub_path = event_path / rel_path
                                wd = self._path_to_wd.pop(path)
                                self._path_to_wd[new_sub_path] = wd
                                self._wd_to_path[wd] = new_sub_path
                            except ValueError:
                                continue
            else:
                # Treat as created if no matching cookie
                if is_dir and self.recursive:
                    try:
                        self._register_path(event_path)
                    except Exception:
                        pass
                self.on_raw_event("created", event_path, is_dir)

    def _expire_cookie(self, cookie: int):
        if cookie in self._move_cookies:
            old_path, _, is_dir = self._move_cookies.pop(cookie)
            # Cookie expired -> fallback to deleted event
            self.on_raw_event("deleted", old_path, is_dir)
