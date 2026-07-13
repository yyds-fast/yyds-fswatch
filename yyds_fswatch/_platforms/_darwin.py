# -*- coding: utf-8 -*-
import sys
if sys.platform != "darwin":
    raise ImportError("macOS only")

import ctypes
import ctypes.util
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
kFSEventStreamEventFlagItemModified = 0x00001000
kFSEventStreamEventFlagItemRenamed = 0x00000800
kFSEventStreamEventFlagItemIsDir = 0x00020000

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
        import asyncio
        super().__init__(paths, recursive, loop, on_raw_event)
        self._ignore_filter = ignore_filter
        self._stream = None
        self._run_loop = None
        self._thread = None
        self._callback_ref = None

    def start(self):
        # We must keep a reference to the callback function to prevent garbage collection
        self._callback_ref = FSEventStreamCallback(self._py_callback)
        
        self._thread = threading.Thread(target=self._run_stream)
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        if self._run_loop:
            CFRunLoopStop(self._run_loop)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._stream = None
        self._run_loop = None
        self._thread = None
        self._callback_ref = None

    def _run_stream(self):
        # Resolve CFStrings for all paths
        c_strings = []
        for path in self.paths:
            path_str = str(path.resolve()).encode('utf-8')
            cf_str = CFStringCreateWithCString(None, path_str, kCFStringEncodingUTF8)
            c_strings.append(cf_str)
            
        # Create CFArray of CFStrings
        paths_array = (ctypes.c_void_p * len(c_strings))(*c_strings)
        cf_paths = CFArrayCreate(None, paths_array, len(c_strings), None)
        
        # Latency of events (seconds)
        latency = 0.01
        
        flags = kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagNoDefer
        
        # Create FSEventStream
        self._stream = FSEventStreamCreate(
            None,
            self._callback_ref,
            None,
            cf_paths,
            kFSEventStreamEventIdSinceNow,
            latency,
            flags
        )
        
        # Release the CFType refs we created
        CFRelease(cf_paths)
        for cf_str in c_strings:
            CFRelease(cf_str)
            
        if not self._stream:
            raise OSError("FSEventStreamCreate failed")
            
        self._run_loop = CFRunLoopGetCurrent()
        
        # Schedule with loop Mode (Default CFRunLoopMode)
        # CFRunLoopCommonModes or kCFRunLoopDefaultMode
        default_mode = CFStringCreateWithCString(None, b"kCFRunLoopDefaultMode", kCFStringEncodingUTF8)
        FSEventStreamScheduleWithRunLoop(self._stream, self._run_loop, default_mode)
        CFRelease(default_mode)
        
        if not FSEventStreamStart(self._stream):
            raise OSError("FSEventStreamStart failed")
            
        #CFRunLoopRun blocks this background thread
        CFRunLoopRun()
        
        # Clean up on exit
        FSEventStreamStop(self._stream)
        FSEventStreamInvalidate(self._stream)
        FSEventStreamRelease(self._stream)

    def _py_callback(self, streamRef, clientCallBackInfo, numEvents, eventPaths, eventFlags, eventIds):
        paths_ptr = ctypes.cast(eventPaths, ctypes.POINTER(ctypes.c_char_p))
        rename_candidates = []
        
        for i in range(numEvents):
            path_str = paths_ptr[i].decode('utf-8', errors='replace')
            path = Path(path_str)
            flags = eventFlags[i]
            
            is_dir = bool(flags & kFSEventStreamEventFlagItemIsDir)
            
            # Match flags to raw events
            if flags & kFSEventStreamEventFlagItemRenamed:
                # Store rename events to pair them up afterwards
                rename_candidates.append((path, is_dir))
            elif flags & kFSEventStreamEventFlagItemCreated:
                self.on_raw_event("created", path, is_dir)
            elif flags & kFSEventStreamEventFlagItemRemoved:
                self.on_raw_event("deleted", path, is_dir)
            elif flags & kFSEventStreamEventFlagItemModified:
                self.on_raw_event("modified", path, is_dir)
                
        # Handle rename candidates pairing
        # In FSEvents, moves inside the directory stream produce 2 renamed events sequentially.
        # The first is the old path (source) and the second is the new path (destination).
        idx = 0
        while idx < len(rename_candidates):
            if idx + 1 < len(rename_candidates):
                old_path, old_is_dir = rename_candidates[idx]
                new_path, _ = rename_candidates[idx+1]
                self.on_raw_event("moved", old_path, old_is_dir, dest_path=new_path)
                idx += 2
            else:
                # Single rename candidate remains (moved out or moved in)
                path, is_dir = rename_candidates[idx]
                if path.exists():
                    # If it exists, it was moved into the watched directory -> created
                    self.on_raw_event("created", path, is_dir)
                else:
                    # If it doesn't exist, it was moved out -> deleted
                    self.on_raw_event("deleted", path, is_dir)
                idx += 1
