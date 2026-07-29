# -*- coding: utf-8 -*-
import sys
if sys.platform != "win32":
    raise ImportError("Windows only")

import asyncio
import ctypes
from ctypes import wintypes
import os
import struct
import threading
from pathlib import Path
from typing import List, Callable

from ._base import BaseWatcher

# Load DLL
kernel32 = ctypes.windll.kernel32

# Win32 Constants
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x0001
FILE_SHARE_WRITE = 0x0002
FILE_SHARE_DELETE = 0x0004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

FILE_NOTIFY_CHANGE_FILE_NAME = 0x0001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x0002
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x0010
FILE_NOTIFY_CHANGE_SIZE = 0x0008
FILE_NOTIFY_CHANGE_ATTRIBUTES = 0x0004
FILE_NOTIFY_CHANGE_CREATION = 0x0040

# CancelSynchronousIo requires a thread handle opened with this access right.
THREAD_TERMINATE = 0x0001

NOTIFY_FILTER = (FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME |
                 FILE_NOTIFY_CHANGE_LAST_WRITE | FILE_NOTIFY_CHANGE_SIZE |
                 FILE_NOTIFY_CHANGE_ATTRIBUTES | FILE_NOTIFY_CHANGE_CREATION)

INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

# Function Bindings
CreateFileW = kernel32.CreateFileW
CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE
]
CreateFileW.restype = wintypes.HANDLE

ReadDirectoryChangesW = kernel32.ReadDirectoryChangesW
ReadDirectoryChangesW.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
    ctypes.c_void_p
]
ReadDirectoryChangesW.restype = wintypes.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL

GetCurrentThreadId = kernel32.GetCurrentThreadId
GetCurrentThreadId.argtypes = []
GetCurrentThreadId.restype = wintypes.DWORD

OpenThread = kernel32.OpenThread
OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
OpenThread.restype = wintypes.HANDLE

CancelSynchronousIo = kernel32.CancelSynchronousIo
CancelSynchronousIo.argtypes = [wintypes.HANDLE]
CancelSynchronousIo.restype = wintypes.BOOL

class WindowsWatcher(BaseWatcher):
    def __init__(self, paths: List[Path], recursive: bool, loop: asyncio.AbstractEventLoop, 
                 on_raw_event: Callable, ignore_filter=None):
        super().__init__(paths, recursive, loop, on_raw_event)
        self._ignore_filter = ignore_filter
        self._handles: List[wintypes.HANDLE] = []
        self._threads: List[threading.Thread] = []
        self._running = False
        self._stop_event = threading.Event()
        self._directory_paths = set()
        self._dir_lock = threading.Lock()
        self._worker_thread_ids = set()
        self._worker_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running and bool(self._threads) and all(
            thread.is_alive() for thread in self._threads
        )

    def _add_directory_recursive_to_set(self, path: Path):
        path = path.absolute()
        with self._dir_lock:
            self._directory_paths.add(path)
        
        if self.recursive:
            try:
                for root, dirs, files in os.walk(path):
                    for d in dirs:
                        dir_path = Path(root) / d
                        with self._dir_lock:
                            self._directory_paths.add(dir_path.absolute())
            except Exception:
                pass

    def _remove_directory_from_set(self, path: Path):
        path_abs = path.absolute()
        with self._dir_lock:
            self._directory_paths.discard(path_abs)
            to_remove = []
            for directory in self._directory_paths:
                try:
                    directory.relative_to(path_abs)
                except ValueError:
                    continue
                to_remove.append(directory)
            for p in to_remove:
                self._directory_paths.discard(p)

    def _rename_directory_in_set(self, old_path: Path, new_path: Path):
        old_abs = old_path.absolute()
        new_abs = new_path.absolute()
        with self._dir_lock:
            subdirs = []
            for p in self._directory_paths:
                try:
                    rel = p.relative_to(old_abs)
                    subdirs.append((p, new_abs / rel))
                except ValueError:
                    continue
            
            for old_p, new_p in subdirs:
                self._directory_paths.discard(old_p)
                self._directory_paths.add(new_p)
            
            self._directory_paths.discard(old_abs)
            self._directory_paths.add(new_abs)

    def _is_path_directory(self, path: Path) -> bool:
        with self._dir_lock:
            return path.absolute() in self._directory_paths

    def start(self):
        if self._running:
            raise RuntimeError("Windows watcher is already running")
        self._running = True
        self._stop_event = threading.Event()
        self._directory_paths.clear()
        with self._worker_lock:
            self._worker_thread_ids.clear()
        try:
            for path in self.paths:
                path = path.resolve()
                if not path.exists():
                    continue
                if path.is_dir():
                    self._add_directory_recursive_to_set(path)

                hDir = CreateFileW(
                    str(path),
                    FILE_LIST_DIRECTORY,
                    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS,
                    None
                )

                if hDir == INVALID_HANDLE_VALUE:
                    err = kernel32.GetLastError()
                    raise OSError(err, f"CreateFileW failed for {path}: {ctypes.FormatError(err)}")

                self._handles.append(hDir)

                thread = threading.Thread(
                    target=self._watch_handle,
                    args=(hDir, path, self._stop_event),
                    name=f"yyds-fswatch:{path}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
        except Exception:
            self.stop()
            raise

    def stop(self):
        self._running = False
        self._stop_event.set()

        # ReadDirectoryChangesW is issued synchronously in each worker. Closing
        # its directory handle alone does not reliably unblock another thread's
        # pending I/O, which used to make every watcher shutdown wait for the
        # join timeout. Explicitly cancel the workers' synchronous I/O first.
        with self._worker_lock:
            worker_thread_ids = tuple(self._worker_thread_ids)
        for thread_id in worker_thread_ids:
            thread_handle = OpenThread(THREAD_TERMINATE, False, thread_id)
            if thread_handle:
                try:
                    CancelSynchronousIo(thread_handle)
                finally:
                    CloseHandle(thread_handle)

        for h in self._handles:
            if h != INVALID_HANDLE_VALUE:
                CloseHandle(h)
        for t in self._threads:
            t.join(timeout=1.0)
        self._handles.clear()
        self._threads.clear()
        with self._dir_lock:
            self._directory_paths.clear()
        with self._worker_lock:
            self._worker_thread_ids.clear()

    def _watch_handle(self, hDir: wintypes.HANDLE, watch_path: Path,
                      stop_event: threading.Event):
        buf = ctypes.create_string_buffer(65536)
        bytes_returned = wintypes.DWORD(0)
        worker_thread_id = GetCurrentThreadId()

        with self._worker_lock:
            self._worker_thread_ids.add(worker_thread_id)

        # Win32 Error codes for overflow detection
        ERROR_MORE_DATA = 234
        ERROR_NOTIFY_ENUM_DIR = 1022

        try:
            while not stop_event.is_set():
                success = ReadDirectoryChangesW(
                    hDir,
                    buf,
                    ctypes.sizeof(buf),
                    self.recursive,
                    NOTIFY_FILTER,
                    ctypes.byref(bytes_returned),
                    None,
                    None
                )

                if not success:
                    if stop_event.is_set():
                        break
                    err = kernel32.GetLastError()
                    if err in (ERROR_MORE_DATA, ERROR_NOTIFY_ENUM_DIR):
                        # Buffer overflow occurred
                        self.on_raw_event("overflow", watch_path, is_dir=False)
                        continue
                    else:
                        exception = OSError(
                            err,
                            f"ReadDirectoryChangesW failed for {watch_path}: {ctypes.FormatError(err)}",
                        )
                        try:
                            self.loop.call_soon_threadsafe(
                                self.loop.call_exception_handler,
                                {
                                    "message": "yyds-fswatch Windows backend failed",
                                    "exception": exception,
                                },
                            )
                        except RuntimeError:
                            pass
                        break

                if bytes_returned.value == 0:
                    # Buffer overflow occurred (ReadDirectoryChangesW returns success but 0 bytes)
                    self.on_raw_event("overflow", watch_path, is_dir=False)
                    continue

                events_in_packet = []
                offset = 0
                while True:
                    if offset + 12 > bytes_returned.value:
                        break

                    next_offset, action, name_len = struct.unpack_from("III", buf.raw, offset)
                    if offset + 12 + name_len > bytes_returned.value:
                        break

                    name_bytes = buf.raw[offset + 12 : offset + 12 + name_len]
                    name = name_bytes.decode('utf-16le', errors='replace')

                    event_path = watch_path / name
                    events_in_packet.append((action, event_path))

                    if next_offset == 0:
                        break
                    offset += next_offset

                i = 0
                n = len(events_in_packet)
                while i < n:
                    action, event_path = events_in_packet[i]

                    if action == 4: # RENAMED_OLD_NAME
                        if i + 1 < n and events_in_packet[i+1][0] == 5:
                            next_action, next_path = events_in_packet[i+1]
                            is_dir = self._is_path_directory(event_path)
                            if is_dir:
                                self._rename_directory_in_set(event_path, next_path)
                            self.on_raw_event("moved", event_path, is_dir, dest_path=next_path)
                            i += 2
                        else:
                            is_dir = self._is_path_directory(event_path)
                            self.on_raw_event("deleted", event_path, is_dir)
                            i += 1
                    elif action == 5: # RENAMED_NEW_NAME
                        is_dir = event_path.is_dir() if event_path.exists() else False
                        if is_dir:
                            self._add_directory_recursive_to_set(event_path)
                        self.on_raw_event("created", event_path, is_dir)
                        i += 1
                    elif action == 1: # ADDED
                        is_dir = event_path.is_dir() if event_path.exists() else False
                        if is_dir:
                            self._add_directory_recursive_to_set(event_path)
                        self.on_raw_event("created", event_path, is_dir)
                        i += 1
                    elif action == 2: # REMOVED
                        is_dir = self._is_path_directory(event_path)
                        self._remove_directory_from_set(event_path)
                        self.on_raw_event("deleted", event_path, is_dir)
                        i += 1
                    elif action == 3: # MODIFIED
                        is_dir = event_path.is_dir() if event_path.exists() else False
                        self.on_raw_event("modified", event_path, is_dir)
                        i += 1
                    else:
                        i += 1
        finally:
            with self._worker_lock:
                self._worker_thread_ids.discard(worker_thread_id)
