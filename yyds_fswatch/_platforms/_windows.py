# -*- coding: utf-8 -*-
import sys
if sys.platform != "win32":
    raise ImportError("Windows only")

import ctypes
from ctypes import wintypes
import os
import struct
import threading
from pathlib import Path
from typing import List, Callable, Dict

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

class WindowsWatcher(BaseWatcher):
    def __init__(self, paths: List[Path], recursive: bool, loop: asyncio.AbstractEventLoop, 
                 on_raw_event: Callable, ignore_filter=None):
        # We need to import asyncio inside methods to ensure compatibility
        import asyncio
        super().__init__(paths, recursive, loop, on_raw_event)
        self._ignore_filter = ignore_filter
        self._handles: List[wintypes.HANDLE] = []
        self._threads: List[threading.Thread] = []
        self._running = False

    def start(self):
        self._running = True
        for path in self.paths:
            path = path.resolve()
            
            # If path is a file, monitor its parent directory
            watch_path = path if path.is_dir() else path.parent
            
            hDir = CreateFileW(
                str(watch_path),
                FILE_LIST_DIRECTORY,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                None
            )
            
            if hDir == INVALID_HANDLE_VALUE:
                err = kernel32.GetLastError()
                raise OSError(err, f"CreateFileW failed for {watch_path}: {ctypes.FormatError(err)}")
                
            self._handles.append(hDir)
            
            t = threading.Thread(target=self._watch_handle, args=(hDir, watch_path, path.is_file()))
            t.daemon = True
            t.start()
            self._threads.append(t)

    def stop(self):
        self._running = False
        for h in self._handles:
            if h != INVALID_HANDLE_VALUE:
                CloseHandle(h)
        for t in self._threads:
            t.join(timeout=1.0)
        self._handles.clear()
        self._threads.clear()

    def _watch_handle(self, hDir: wintypes.HANDLE, watch_path: Path, is_single_file: bool):
        buf = ctypes.create_string_buffer(65536)
        bytes_returned = wintypes.DWORD(0)
        last_rename_old = None

        while self._running:
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
            
            if not success or not self._running:
                break
                
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
                
                # If we are only monitoring a single file, filter out other files
                if is_single_file and event_path.name != watch_path.name:
                    if next_offset == 0:
                        break
                    offset += next_offset
                    continue
                
                is_dir = event_path.is_dir() if event_path.exists() else False
                
                if action == 1: # ADDED
                    self.on_raw_event("created", event_path, is_dir)
                elif action == 2: # REMOVED
                    self.on_raw_event("deleted", event_path, is_dir)
                elif action == 3: # MODIFIED
                    self.on_raw_event("modified", event_path, is_dir)
                elif action == 4: # RENAMED_OLD_NAME
                    last_rename_old = (event_path, is_dir)
                elif action == 5: # RENAMED_NEW_NAME
                    if last_rename_old:
                        old_path, old_is_dir = last_rename_old
                        self.on_raw_event("moved", old_path, old_is_dir, dest_path=event_path)
                        last_rename_old = None
                    else:
                        self.on_raw_event("created", event_path, is_dir)
                
                if next_offset == 0:
                    break
                offset += next_offset
