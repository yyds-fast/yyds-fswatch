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
from typing import List, Callable, Dict, Tuple

from ..ignore import path_prefix_pair, subtree_relative_suffix
from ._base import BaseWatcher

# Load DLL with thread-local Win32 LastError preservation
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Win32 Constants
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x0001
FILE_SHARE_WRITE = 0x0002
FILE_SHARE_DELETE = 0x0004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000

FILE_NOTIFY_CHANGE_FILE_NAME = 0x0001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x0002
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x0010
FILE_NOTIFY_CHANGE_SIZE = 0x0008
FILE_NOTIFY_CHANGE_ATTRIBUTES = 0x0004
FILE_NOTIFY_CHANGE_CREATION = 0x0040

ERROR_MORE_DATA = 234
ERROR_OPERATION_ABORTED = 995
ERROR_IO_PENDING = 997
ERROR_NOTIFY_ENUM_DIR = 1022

WAIT_OBJECT_0 = 0x00000000
INFINITE = 0xFFFFFFFF

NOTIFY_FILTER = (FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME |
                 FILE_NOTIFY_CHANGE_LAST_WRITE | FILE_NOTIFY_CHANGE_SIZE |
                 FILE_NOTIFY_CHANGE_ATTRIBUTES | FILE_NOTIFY_CHANGE_CREATION)

INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


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
    ctypes.POINTER(OVERLAPPED),
    ctypes.c_void_p
]
ReadDirectoryChangesW.restype = wintypes.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL

GetCurrentThreadId = kernel32.GetCurrentThreadId
GetCurrentThreadId.argtypes = []
GetCurrentThreadId.restype = wintypes.DWORD

CreateEventW = kernel32.CreateEventW
CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
CreateEventW.restype = wintypes.HANDLE

SetEvent = kernel32.SetEvent
SetEvent.argtypes = [wintypes.HANDLE]
SetEvent.restype = wintypes.BOOL

ResetEvent = kernel32.ResetEvent
ResetEvent.argtypes = [wintypes.HANDLE]
ResetEvent.restype = wintypes.BOOL

WaitForMultipleObjects = kernel32.WaitForMultipleObjects
WaitForMultipleObjects.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.BOOL,
    wintypes.DWORD,
]
WaitForMultipleObjects.restype = wintypes.DWORD

GetOverlappedResult = kernel32.GetOverlappedResult
GetOverlappedResult.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(OVERLAPPED),
    ctypes.POINTER(wintypes.DWORD),
    wintypes.BOOL,
]
GetOverlappedResult.restype = wintypes.BOOL

CancelIo = kernel32.CancelIo
CancelIo.argtypes = [wintypes.HANDLE]
CancelIo.restype = wintypes.BOOL

CancelIoEx = kernel32.CancelIoEx
CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
CancelIoEx.restype = wintypes.BOOL

class WindowsWatcher(BaseWatcher):
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
        self._handles: List[wintypes.HANDLE] = []
        self._threads: List[threading.Thread] = []
        self._watched_inodes: Dict[Path, Tuple[int, int]] = {}
        self._starting = False
        self._running = False
        self._stop_event = threading.Event()
        self._stop_win_event = None
        self._directory_paths = set()
        self._dir_lock = threading.Lock()
        self._worker_thread_ids = set()
        self._worker_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running and bool(self._threads) and all(
            thread.is_alive() for thread in self._threads
        )

    def is_healthy_for(self, paths: List[Path]) -> bool:
        if self._starting and self._running:
            return True
        if not self.is_running:
            return False
        for path in paths:
            resolved = path.resolve()
            try:
                st = os.stat(resolved)
            except OSError:
                continue
            if resolved not in self._watched_inodes:
                return False
            expected = self._watched_inodes.get(resolved)
            if expected is not None and expected[1] and st.st_ino and (st.st_dev, st.st_ino) != expected:
                return False
        return True

    def _add_directory_recursive_to_set(self, path: Path, recurse: bool = True):
        path_abs = path.absolute()
        found = {path_abs}

        if recurse:
            try:
                for root, dirs, files in os.walk(path):
                    if self._stop_event.is_set():
                        return
                    root_path = Path(root)
                    filt = self._ignore_filter
                    ignore_fn = getattr(filt, "is_ignored", None) if filt is not None else None
                    if ignore_fn is not None:
                        if ignore_fn(root_path, is_dir=True):
                            dirs[:] = []
                            continue
                        dirs[:] = [
                            d for d in dirs
                            if not ignore_fn(root_path / d, is_dir=True)
                        ]
                    for d in dirs:
                        found.add((root_path / d).absolute())
            except Exception:
                pass

        with self._dir_lock:
            self._directory_paths.update(found)

    def _reconcile_watches_sync(self):
        if not self._running or not self.recursive or not self._recursive_roots:
            return
        filt = self._ignore_filter
        ignore_fn = getattr(filt, "is_ignored", None) if filt is not None else None
        if ignore_fn is not None:
            with self._dir_lock:
                candidates = list(self._directory_paths)
            ignored_dirs = [
                d for d in candidates
                if d not in self._watched_roots and ignore_fn(d, True)
            ]
            if ignored_dirs:
                with self._dir_lock:
                    self._directory_paths.difference_update(ignored_dirs)
        for path in self.paths:
            if self._stop_event.is_set():
                break
            resolved = path.resolve()
            if resolved.is_dir() and self._should_recurse_into(resolved, resolved=True):
                self._add_directory_recursive_to_set(resolved, recurse=True)

    def reconcile_watches(self):
        if not self._running or not self.recursive or not self._recursive_roots:
            return
        if self.loop is not None and self.loop.is_running():
            threading.Thread(
                target=self._reconcile_watches_sync,
                name="yyds-fswatch-win32-reconcile",
                daemon=True,
            ).start()
        else:
            self._reconcile_watches_sync()

    def _remove_directory_from_set(self, path: Path):
        exact_norm, prefix_norm = path_prefix_pair(path.absolute())
        with self._dir_lock:
            to_remove = [
                directory for directory in self._directory_paths
                if subtree_relative_suffix(directory, exact_norm, prefix_norm) is not None
            ]
            for p in to_remove:
                self._directory_paths.discard(p)

    def _rename_directory_in_set(self, old_path: Path, new_path: Path):
        old_abs = old_path.absolute()
        new_abs = new_path.absolute()
        exact_norm, prefix_norm = path_prefix_pair(old_abs)
        with self._dir_lock:
            subdirs = []
            for p in self._directory_paths:
                suffix = subtree_relative_suffix(p, exact_norm, prefix_norm)
                if suffix is None:
                    continue
                dest = new_abs / suffix if suffix else new_abs
                subdirs.append((p, dest))
            
            for old_p, new_p in subdirs:
                self._directory_paths.discard(old_p)
                self._directory_paths.add(new_p)

    def _is_path_directory(self, path: Path) -> bool:
        with self._dir_lock:
            return path.absolute() in self._directory_paths

    def _report_backend_error(self, watch_path: Path, err: int):
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

    def start(self):
        if self._running:
            raise RuntimeError("Windows watcher is already running")
        self._running = True
        self._stop_event = threading.Event()
        self._watched_inodes.clear()
        with self._dir_lock:
            self._directory_paths.clear()
        with self._worker_lock:
            self._worker_thread_ids.clear()
        try:
            stop_win_event = CreateEventW(None, True, False, None)
            if not stop_win_event:
                err = ctypes.get_last_error()
                raise OSError(err, f"CreateEventW failed: {ctypes.FormatError(err)}")
            self._stop_win_event = stop_win_event

            for path in self.paths:
                path = path.resolve()
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                watch_recursive = self._should_recurse_into(path, resolved=True)
                if path.is_dir():
                    self._add_directory_recursive_to_set(path, recurse=watch_recursive)

                hDir = CreateFileW(
                    str(path),
                    FILE_LIST_DIRECTORY,
                    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED,
                    None
                )

                if hDir in (INVALID_HANDLE_VALUE, None):
                    err = ctypes.get_last_error()
                    raise OSError(err, f"CreateFileW failed for {path}: {ctypes.FormatError(err)}")

                self._handles.append(hDir)
                self._watched_inodes[path] = (st.st_dev, st.st_ino)

                worker_ready = threading.Event()
                worker_error = []
                thread = threading.Thread(
                    target=self._watch_handle,
                    args=(
                        hDir,
                        path,
                        self._stop_event,
                        stop_win_event,
                        watch_recursive,
                        worker_ready,
                        worker_error,
                    ),
                    name=f"yyds-fswatch:{path}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
                if not worker_ready.wait(timeout=5.0):
                    raise TimeoutError(f"ReadDirectoryChangesW did not arm within 5s for {path}")
                if worker_error:
                    raise worker_error[0]
        except Exception:
            self.stop()
            raise

    async def astart(self):
        self._starting = True
        try:
            await self._run_in_helper_thread(self.start, "yyds-fswatch-win32-start")
        finally:
            self._starting = False

    def stop(self):
        self._running = False
        self._stop_event.set()
        if self._stop_win_event:
            SetEvent(self._stop_win_event)
        for h in self._handles:
            if h not in (INVALID_HANDLE_VALUE, None):
                CancelIoEx(h, None)

        for t in self._threads:
            t.join(timeout=1.0)
        alive = any(t.is_alive() for t in self._threads)
        for h in self._handles:
            if h not in (INVALID_HANDLE_VALUE, None):
                CloseHandle(h)
        if self._stop_win_event and not alive:
            CloseHandle(self._stop_win_event)
            self._stop_win_event = None
        self._handles.clear()
        self._threads.clear()
        self._watched_inodes.clear()
        with self._dir_lock:
            self._directory_paths.clear()
        with self._worker_lock:
            self._worker_thread_ids.clear()

    async def aclose(self):
        await self._run_in_helper_thread(self.stop, "yyds-fswatch-win32-stop")

    def _watch_handle(self, hDir: wintypes.HANDLE, watch_path: Path,
                      stop_event: threading.Event, stop_win_event: wintypes.HANDLE,
                      watch_recursive: bool, worker_ready: threading.Event,
                      worker_error: list):
        buf = ctypes.create_string_buffer(65536)
        bytes_returned = wintypes.DWORD(0)
        worker_thread_id = GetCurrentThreadId()

        with self._worker_lock:
            self._worker_thread_ids.add(worker_thread_id)

        io_event = CreateEventW(None, True, False, None)
        if not io_event:
            err = ctypes.get_last_error()
            worker_error.append(OSError(err, f"CreateEventW failed: {ctypes.FormatError(err)}"))
            worker_ready.set()
            with self._worker_lock:
                self._worker_thread_ids.discard(worker_thread_id)
            return

        overlapped = OVERLAPPED()
        wait_handles = (wintypes.HANDLE * 2)(io_event, stop_win_event)
        io_pending = False
        recent_ignored_adds: Dict[Path, Path] = {}

        try:
            while not stop_event.is_set():
                ResetEvent(io_event)
                overlapped.Internal = 0
                overlapped.InternalHigh = 0
                overlapped.Offset = 0
                overlapped.OffsetHigh = 0
                overlapped.hEvent = io_event

                issued = ReadDirectoryChangesW(
                    hDir,
                    buf,
                    ctypes.sizeof(buf),
                    watch_recursive,
                    NOTIFY_FILTER,
                    None,
                    ctypes.byref(overlapped),
                    None
                )
                err = 0 if issued else ctypes.get_last_error()
                if not issued and err != ERROR_IO_PENDING:
                    if not worker_ready.is_set():
                        worker_error.append(
                            OSError(
                                err,
                                f"ReadDirectoryChangesW failed for {watch_path}: {ctypes.FormatError(err)}",
                            )
                        )
                        worker_ready.set()
                        break
                    if stop_event.is_set() or err == ERROR_OPERATION_ABORTED:
                        break
                    if err in (ERROR_MORE_DATA, ERROR_NOTIFY_ENUM_DIR):
                        self.on_raw_event("overflow", watch_path, is_dir=False)
                        continue
                    self._report_backend_error(watch_path, err)
                    break

                io_pending = True
                if not worker_ready.is_set():
                    # Kernel directory-change buffer is now armed on hDir.
                    worker_ready.set()

                # ReadDirectoryChangesW returns TRUE even when NtNotifyChangeDirectoryFile
                # returns STATUS_PENDING, so always wait on [io_event, stop_win_event].
                wait_rc = WaitForMultipleObjects(2, wait_handles, False, INFINITE)
                if wait_rc == WAIT_OBJECT_0 + 1 or stop_event.is_set():
                    break
                if wait_rc != WAIT_OBJECT_0:
                    werr = ctypes.get_last_error()
                    if not stop_event.is_set():
                        self._report_backend_error(watch_path, werr)
                    break

                ok = GetOverlappedResult(
                    hDir,
                    ctypes.byref(overlapped),
                    ctypes.byref(bytes_returned),
                    False,
                )
                io_pending = False
                if not ok:
                    gerr = ctypes.get_last_error()
                    if stop_event.is_set() or gerr == ERROR_OPERATION_ABORTED:
                        break
                    if gerr in (ERROR_MORE_DATA, ERROR_NOTIFY_ENUM_DIR):
                        self.on_raw_event("overflow", watch_path, is_dir=False)
                        continue
                    self._report_backend_error(watch_path, gerr)
                    break

                raw_size = bytes_returned.value
                if raw_size == 0:
                    # Buffer overflow occurred (ReadDirectoryChangesW returns success but 0 bytes)
                    self.on_raw_event("overflow", watch_path, is_dir=False)
                    continue

                packet_bytes = buf.raw[:raw_size]
                events_in_packet = []
                offset = 0
                while True:
                    if offset + 12 > raw_size:
                        break

                    next_offset, action, name_len = struct.unpack_from("III", packet_bytes, offset)
                    if offset + 12 + name_len > raw_size:
                        break

                    name_bytes = packet_bytes[offset + 12 : offset + 12 + name_len]
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
                        if "~" in event_path.name and not event_path.exists():
                            cand = recent_ignored_adds.get(event_path.parent)
                            if cand is not None and not cand.exists():
                                recent_ignored_adds.pop(event_path.parent, None)
                                event_path = cand
                        if i + 1 < n and events_in_packet[i+1][0] == 5:
                            next_action, next_path = events_in_packet[i+1]
                            was_tracked_dir = self._is_path_directory(event_path)
                            is_dir = was_tracked_dir or next_path.is_dir()
                            if is_dir:
                                if was_tracked_dir:
                                    self._rename_directory_in_set(event_path, next_path)
                                else:
                                    self._add_directory_recursive_to_set(next_path, recurse=watch_recursive)
                            if not self._should_skip_event("moved", event_path, is_dir, dest_path=next_path):
                                self.on_raw_event("moved", event_path, is_dir, dest_path=next_path)
                            i += 2
                        else:
                            is_dir = self._is_path_directory(event_path)
                            self._remove_directory_from_set(event_path)
                            if not self._should_skip_event("deleted", event_path, is_dir):
                                self.on_raw_event("deleted", event_path, is_dir)
                            i += 1
                    elif action == 5: # RENAMED_NEW_NAME
                        is_dir = event_path.is_dir()
                        if is_dir:
                            self._add_directory_recursive_to_set(event_path, recurse=watch_recursive)
                        if not self._should_skip_event("created", event_path, is_dir):
                            self.on_raw_event("created", event_path, is_dir)
                        i += 1
                    elif action == 1: # ADDED
                        is_dir = event_path.is_dir()
                        if is_dir:
                            self._add_directory_recursive_to_set(event_path, recurse=watch_recursive)
                        if not self._should_skip_event("created", event_path, is_dir):
                            self.on_raw_event("created", event_path, is_dir)
                        elif not is_dir:
                            if len(recent_ignored_adds) >= 64:
                                recent_ignored_adds.clear()
                            recent_ignored_adds[event_path.parent] = event_path
                        i += 1
                    elif action == 2: # REMOVED
                        if recent_ignored_adds.get(event_path.parent) == event_path:
                            recent_ignored_adds.pop(event_path.parent, None)
                        is_dir = self._is_path_directory(event_path)
                        # MoveFileExW(MOVEFILE_REPLACE_EXISTING) emits REMOVED
                        # on the destination right before RENAMED_OLD + RENAMED_NEW.
                        if (
                            not is_dir and
                            i + 2 < n and
                            events_in_packet[i + 1][0] == 4 and
                            events_in_packet[i + 2][0] == 5 and
                            events_in_packet[i + 2][1] == event_path
                        ):
                            i += 1
                            continue
                        self._remove_directory_from_set(event_path)
                        if not self._should_skip_event("deleted", event_path, is_dir):
                            self.on_raw_event("deleted", event_path, is_dir)
                        i += 1
                    elif action == 3: # MODIFIED
                        is_dir = self._is_path_directory(event_path) or event_path.is_dir()
                        if not self._should_skip_event("modified", event_path, is_dir):
                            self.on_raw_event("modified", event_path, is_dir)
                        i += 1
                    else:
                        i += 1
        finally:
            if io_pending:
                CancelIo(hDir)
                GetOverlappedResult(
                    hDir,
                    ctypes.byref(overlapped),
                    ctypes.byref(bytes_returned),
                    True,
                )
            CloseHandle(io_event)
            if not worker_ready.is_set():
                worker_ready.set()
            with self._worker_lock:
                self._worker_thread_ids.discard(worker_thread_id)
