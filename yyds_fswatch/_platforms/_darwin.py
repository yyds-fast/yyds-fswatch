# -*- coding: utf-8 -*-
import sys
if sys.platform != "darwin":
    raise ImportError("macOS only")

import asyncio
import ctypes
import ctypes.util
import os
import threading
from pathlib import Path
from typing import List, Callable

from ._base import BaseWatcher

# Load Libraries
cf_path = ctypes.util.find_library('CoreFoundation') or '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation'
cs_path = ctypes.util.find_library('CoreServices') or '/System/Library/Frameworks/CoreServices.framework/CoreServices'

try:
    cf = ctypes.CDLL(cf_path, use_errno=True)
    cs = ctypes.CDLL(cs_path, use_errno=True)
except Exception as e:
    raise ImportError(f"Failed to load macOS CoreFoundation/CoreServices: {e}")

# Types & Signatures
CFStringCreateWithCString = cf.CFStringCreateWithCString
CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
CFStringCreateWithCString.restype = ctypes.c_void_p

CFArrayCreate = cf.CFArrayCreate
CFArrayCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_long, ctypes.c_void_p]
CFArrayCreate.restype = ctypes.c_void_p

CFRelease = cf.CFRelease
CFRelease.argtypes = [ctypes.c_void_p]
CFRelease.restype = None

kCFStringEncodingUTF8 = 0x08000100

FSEventStreamCreate = cs.FSEventStreamCreate
FSEventStreamCreate.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint64,
    ctypes.c_double,
    ctypes.c_uint32
]
FSEventStreamCreate.restype = ctypes.c_void_p

FSEventStreamScheduleWithRunLoop = cs.FSEventStreamScheduleWithRunLoop
FSEventStreamScheduleWithRunLoop.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
FSEventStreamScheduleWithRunLoop.restype = None

FSEventStreamStart = cs.FSEventStreamStart
FSEventStreamStart.argtypes = [ctypes.c_void_p]
FSEventStreamStart.restype = ctypes.c_bool

FSEventStreamStop = cs.FSEventStreamStop
FSEventStreamStop.argtypes = [ctypes.c_void_p]
FSEventStreamStop.restype = None

FSEventStreamInvalidate = cs.FSEventStreamInvalidate
FSEventStreamInvalidate.argtypes = [ctypes.c_void_p]
FSEventStreamInvalidate.restype = None

FSEventStreamRelease = cs.FSEventStreamRelease
FSEventStreamRelease.argtypes = [ctypes.c_void_p]
FSEventStreamRelease.restype = None

CFRunLoopGetCurrent = cf.CFRunLoopGetCurrent
CFRunLoopGetCurrent.argtypes = []
CFRunLoopGetCurrent.restype = ctypes.c_void_p

CFRunLoopRun = cf.CFRunLoopRun
CFRunLoopRun.argtypes = []
CFRunLoopRun.restype = None

CFRunLoopStop = cf.CFRunLoopStop
CFRunLoopStop.argtypes = [ctypes.c_void_p]
CFRunLoopStop.restype = None

# FSEvents Constants
kFSEventStreamEventIdSinceNow = 0xFFFFFFFFFFFFFFFF
kFSEventStreamCreateFlagFileEvents = 0x00000010
kFSEventStreamCreateFlagNoDefer = 0x00000002

kFSEventStreamEventFlagItemCreated = 0x00000100
kFSEventStreamEventFlagItemRemoved = 0x00000200
kFSEventStreamEventFlagItemInodeMetaMod = 0x00000400
kFSEventStreamEventFlagItemModified = 0x00001000
kFSEventStreamEventFlagItemFinderInfoMod = 0x00002000
kFSEventStreamEventFlagItemChangeOwner = 0x00004000
kFSEventStreamEventFlagItemXattrMod = 0x00008000
kFSEventStreamEventFlagItemRenamed = 0x00000800
kFSEventStreamEventFlagItemIsDir = 0x00020000
kFSEventStreamEventFlagMustScanSubDirs = 0x00000001
kFSEventStreamEventFlagUserDropped = 0x00000002
kFSEventStreamEventFlagKernelDropped = 0x00000004
kFSEventStreamEventFlagEventIdsWrapped = 0x00000008

# Callback Type
FSEventStreamCallback = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p, # streamRef
    ctypes.c_void_p, # clientCallBackInfo
    ctypes.c_size_t, # numEvents
    ctypes.c_void_p, # eventPaths
    ctypes.POINTER(ctypes.c_uint32), # eventFlags
    ctypes.POINTER(ctypes.c_uint64)  # eventIds
)

class DarwinWatcher(BaseWatcher):
    def __init__(self, paths: List[Path], recursive: bool, loop: asyncio.AbstractEventLoop, 
                 on_raw_event: Callable, ignore_filter=None):
        super().__init__(
            paths,
            recursive,
            loop,
            on_raw_event,
            ignore_filter=ignore_filter,
        )
        self._stream = None
        self._run_loop = None
        self._thread = None
        self._callback_ref = None
        self._started_event = threading.Event()
        self._startup_error = None
        self._stop_requested = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._startup_error is None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("macOS watcher is already running")
        self._started_event.clear()
        self._stop_requested.clear()
        self._startup_error = None
        # We must keep a reference to the callback function to prevent garbage collection
        self._callback_ref = FSEventStreamCallback(self._py_callback)
        
        self._thread = threading.Thread(
            target=self._run_stream,
            name="yyds-fswatch-fsevents",
            daemon=True,
        )
        self._thread.start()
        if not self._started_event.wait(timeout=5.0):
            self.stop()
            raise TimeoutError("FSEvents stream did not start within 5 seconds")
        if self._startup_error is not None:
            self.stop()
            raise RuntimeError("FSEvents stream failed to start") from self._startup_error

    def stop(self):
        self._stop_requested.set()
        if self._run_loop:
            CFRunLoopStop(self._run_loop)
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._thread is not None and self._thread.is_alive():
            # Keep callback and stream references alive: releasing them while C
            # may still invoke the callback can crash the interpreter.
            return
        self._stream = None
        self._run_loop = None
        self._thread = None
        self._callback_ref = None

    def _run_stream(self):
        try:
            self._run_stream_impl()
        except Exception as exc:
            self._startup_error = exc
            if self._started_event.is_set():
                try:
                    self.loop.call_soon_threadsafe(
                        self.loop.call_exception_handler,
                        {"message": "yyds-fswatch FSEvents stream failed", "exception": exc},
                    )
                except RuntimeError:
                    pass
        finally:
            self._started_event.set()

    def _run_stream_impl(self):
        c_strings = []
        cf_paths = None
        stream_started = False
        try:
            for path in self.paths:
                if not path.exists():
                    continue
                path_str = str(path.resolve()).encode("utf-8")
                cf_string = CFStringCreateWithCString(None, path_str, kCFStringEncodingUTF8)
                if not cf_string:
                    raise OSError(f"CFStringCreateWithCString failed for {path}")
                c_strings.append(cf_string)

            if not c_strings:
                raise FileNotFoundError("no watch path currently exists")

            paths_array = (ctypes.c_void_p * len(c_strings))(*c_strings)
            cf_paths = CFArrayCreate(None, paths_array, len(c_strings), None)
            if not cf_paths:
                raise OSError("CFArrayCreate failed")

            flags = kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagNoDefer
            self._stream = FSEventStreamCreate(
                None,
                self._callback_ref,
                None,
                cf_paths,
                kFSEventStreamEventIdSinceNow,
                0.01,
                flags,
            )
            if not self._stream:
                raise OSError("FSEventStreamCreate failed")

            # FSEventStreamCreate retains the path array.
            CFRelease(cf_paths)
            cf_paths = None
            for cf_string in c_strings:
                CFRelease(cf_string)
            c_strings.clear()

            self._run_loop = CFRunLoopGetCurrent()
            default_mode = CFStringCreateWithCString(
                None,
                b"kCFRunLoopDefaultMode",
                kCFStringEncodingUTF8,
            )
            if not default_mode:
                raise OSError("failed to create the FSEvents run-loop mode")
            try:
                FSEventStreamScheduleWithRunLoop(self._stream, self._run_loop, default_mode)
            finally:
                CFRelease(default_mode)

            if not FSEventStreamStart(self._stream):
                raise OSError("FSEventStreamStart failed")
            stream_started = True
            self._started_event.set()

            if not self._stop_requested.is_set():
                CFRunLoopRun()
        finally:
            if cf_paths:
                CFRelease(cf_paths)
            for cf_string in c_strings:
                CFRelease(cf_string)
            if self._stream:
                if stream_started:
                    FSEventStreamStop(self._stream)
                FSEventStreamInvalidate(self._stream)
                FSEventStreamRelease(self._stream)
            self._stream = None
            self._run_loop = None

    def _emit_if_visible(self, event_type: str, path: Path, is_dir: bool,
                         dest_path: Path = None):
        if not self._should_skip_event(event_type, path, is_dir, dest_path=dest_path):
            if dest_path is not None:
                self.on_raw_event(event_type, path, is_dir, dest_path=dest_path)
            else:
                self.on_raw_event(event_type, path, is_dir)

    def _py_callback(self, streamRef, clientCallBackInfo, numEvents, eventPaths, eventFlags, eventIds):
        paths_ptr = ctypes.cast(eventPaths, ctypes.POINTER(ctypes.c_char_p))
        rename_candidates = []
        modify_flags = (
            kFSEventStreamEventFlagItemModified |
            kFSEventStreamEventFlagItemInodeMetaMod |
            kFSEventStreamEventFlagItemFinderInfoMod |
            kFSEventStreamEventFlagItemChangeOwner |
            kFSEventStreamEventFlagItemXattrMod
        )
        
        for i in range(numEvents):
            path_str = os.fsdecode(paths_ptr[i])
            path = Path(path_str)
            flags = eventFlags[i]
            
            is_dir = bool(flags & kFSEventStreamEventFlagItemIsDir)

            if flags & (kFSEventStreamEventFlagMustScanSubDirs |
                        kFSEventStreamEventFlagUserDropped |
                        kFSEventStreamEventFlagKernelDropped |
                        kFSEventStreamEventFlagEventIdsWrapped):
                self.on_raw_event("overflow", path, False)
                continue
            
            # FSEvents coalesces multiple flags onto one record; avoid dropping
            # removals when ItemCreated and ItemRemoved appear together.
            if flags & kFSEventStreamEventFlagItemRenamed:
                # Store rename events to pair them up afterwards
                rename_candidates.append((path, is_dir))
                continue

            created = bool(flags & kFSEventStreamEventFlagItemCreated)
            removed = bool(flags & kFSEventStreamEventFlagItemRemoved)
            modified = bool(flags & modify_flags)

            if created and removed:
                if path.exists():
                    self._emit_if_visible("deleted", path, is_dir)
                    self._emit_if_visible("created", path, is_dir)
                else:
                    self._emit_if_visible("created", path, is_dir)
                    self._emit_if_visible("deleted", path, is_dir)
            elif created:
                self._emit_if_visible("created", path, is_dir)
            elif removed:
                self._emit_if_visible("deleted", path, is_dir)
            elif modified:
                self._emit_if_visible("modified", path, is_dir)
                
        # FSEvents has no rename cookie. Pair missing paths with existing paths
        # of matching entry kind in the same batch; positional zipping without
        # matching is_dir corrupts simultaneous file and directory renames.
        removed_entries = [(path, is_dir) for path, is_dir in rename_candidates if not path.exists()]
        added_entries = [(path, is_dir) for path, is_dir in rename_candidates if path.exists()]
        matched_added = set()
        for old_path, old_is_dir in removed_entries:
            matched_index = None
            for idx, (new_path, new_is_dir) in enumerate(added_entries):
                if idx not in matched_added and new_is_dir == old_is_dir:
                    matched_index = idx
                    break
            if matched_index is not None:
                matched_added.add(matched_index)
                new_path, _ = added_entries[matched_index]
                self._emit_if_visible("moved", old_path, old_is_dir, dest_path=new_path)
            else:
                self._emit_if_visible("deleted", old_path, old_is_dir)
        for idx, (path, is_dir) in enumerate(added_entries):
            if idx not in matched_added:
                self._emit_if_visible("created", path, is_dir)
