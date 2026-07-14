# -*- coding: utf-8 -*-

import asyncio
import queue
import threading
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Union, List, Tuple, Callable, Dict

from .events import FSEvent
from .ignore import GitIgnoreFilter

_STOP_SENTINEL = object()

@dataclass
class PendingState:
    event_type: str
    is_directory: bool
    dest_path: str = ""

class FSWatcher:
    def __init__(self, path: Union[str, Path, List[Union[str, Path]], Tuple[Union[str, Path]]], 
                 recursive: bool = True, debounce: float = 0.05, 
                 use_gitignore: bool = True, ignore_patterns: List[str] = None):
        self._is_file_watch = False
        self._file_targets = set()
        self.paths = []

        if isinstance(path, (str, Path)):
            raw_paths = [Path(path)]
        else:
            raw_paths = [Path(p) for p in path]

        for p in raw_paths:
            p_resolved = p.resolve()
            if p_resolved.is_file():
                self._is_file_watch = True
                self._file_targets.add(p_resolved)
                self.paths.append(p_resolved.parent)
            else:
                self.paths.append(p_resolved)

        self.paths = list(dict.fromkeys(self.paths))

        self.recursive = recursive
        self.debounce = debounce
        self.use_gitignore = use_gitignore
        self.ignore_patterns = ignore_patterns

        self._callbacks: Dict[str, List[Callable]] = {}
        self._queue: asyncio.Queue = None
        self._loop: asyncio.AbstractEventLoop = None
        self._watcher = None
        self._ignore_filter = None

        # Debounce state
        self._pending_events: Dict[Path, PendingState] = {}
        self._pending_timers: Dict[Path, asyncio.TimerHandle] = {}

        # Sync iteration state
        self._sync_queue: queue.Queue = None
        self._bg_thread: threading.Thread = None
        self._bg_loop: asyncio.AbstractEventLoop = None
        self._bg_task: asyncio.Task = None
        self.is_ready = False

    def on(self, event_type: str):
        """Decorator to register event callbacks."""
        def decorator(func: Callable):
            # Normalize event_type to match watchdog naming if needed, but keeping lowercase standard
            self._callbacks.setdefault(event_type.lower(), []).append(func)
            return func
        return decorator

    # --- Synchronous Loop Mode (Level 1) ---
    def start(self):
        """Start blocking monitoring loop (Ctrl+C to exit)."""
        try:
            asyncio.run(self._run_decorator_loop())
        except KeyboardInterrupt:
            pass

    async def _run_decorator_loop(self):
        async with self as w:
            async for event in w:
                callbacks = self._callbacks.get(event.event_type.lower(), [])
                callbacks_any = self._callbacks.get('*', []) + self._callbacks.get('any', [])
                
                for cb in callbacks + callbacks_any:
                    try:
                        if asyncio.iscoroutinefunction(cb):
                            await cb(event)
                        else:
                            cb(event)
                    except Exception as e:
                        print(f"Error in yyds-fswatch callback: {e}", file=sys.stderr)

    # --- Synchronous Context & Iterator Mode (Level 3) ---
    def __enter__(self):
        self._sync_queue = queue.Queue()
        self._bg_thread = threading.Thread(target=self._run_bg_loop)
        self._bg_thread.daemon = True
        self._bg_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._bg_loop and self._bg_loop.is_running():
            self._bg_loop.call_soon_threadsafe(self._stop_bg_loop)
        if self._bg_thread:
            self._bg_thread.join(timeout=2.0)
        # Put final sentinel just in case
        self._sync_queue.put(_STOP_SENTINEL)

    def _run_bg_loop(self):
        self._bg_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._bg_loop)
        
        async def run():
            try:
                async with self as w:
                    async for event in w:
                        self._sync_queue.put(event)
            except asyncio.CancelledError:
                pass
            finally:
                self._sync_queue.put(_STOP_SENTINEL)

        self._bg_task = self._bg_loop.create_task(run())
        try:
            self._bg_loop.run_until_complete(self._bg_task)
        finally:
            self._bg_loop.close()

    def _stop_bg_loop(self):
        if self._bg_task:
            self._bg_task.cancel()

    def __iter__(self):
        return self

    def __next__(self):
        event = self._sync_queue.get()
        if event is _STOP_SENTINEL:
            raise StopIteration
        return event

    # --- Asynchronous Context & Iterator Mode (Level 4) ---
    async def __aenter__(self):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        
        # Load Ignore rules
        self._ignore_filter = GitIgnoreFilter(self.paths, self.use_gitignore, self.ignore_patterns)

        # Factory resolve platform watcher
        from ._platforms import get_platform_watcher_class
        watcher_cls = get_platform_watcher_class()
        
        self._watcher = watcher_cls(
            paths=self.paths,
            recursive=self.recursive,
            loop=self._loop,
            on_raw_event=self._on_raw_event,
            ignore_filter=self._ignore_filter
        )
        
        self._watcher.start()
        self.is_ready = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Stop background platform monitoring
        for timer in self._pending_timers.values():
            timer.cancel()
        self._pending_timers.clear()
        self._pending_events.clear()

        if self._watcher:
            self._watcher.stop()
            self._watcher = None

        self.is_ready = False
        self._queue.put_nowait(_STOP_SENTINEL)

    def __aiter__(self):
        return self

    async def __anext__(self) -> FSEvent:
        event = await self._queue.get()
        if event is _STOP_SENTINEL:
            raise StopAsyncIteration
        return event

    # --- Debounce Mechanism ---
    def _on_raw_event(self, event_type: str, path: Path, is_dir: bool, dest_path: Path = None):
        """Called by the platform watcher when a change is detected."""
        self._loop.call_soon_threadsafe(self._push_to_debounce, event_type, path, is_dir, dest_path)

    def _push_to_debounce(self, event_type: str, path: Path, is_dir: bool, dest_path: Path = None):
        # 0. If we are watching specific files, filter out events for other files in the parent directory
        if self._is_file_watch:
            path_resolved = path.resolve()
            dest_resolved = Path(dest_path).resolve() if dest_path else None
            is_target = path_resolved in self._file_targets or \
                        (dest_resolved and dest_resolved in self._file_targets)
            if not is_target:
                return

        # 1. Filter out ignored files/folders
        if self._ignore_filter and self._ignore_filter.is_ignored(path, is_dir):
            return
        if dest_path and self._ignore_filter and self._ignore_filter.is_ignored(dest_path, is_dir):
            return

        # 2. moved events bypass debounce to prevent rename cookie loss/delay
        if event_type == "moved":
            # Flush any pending events for the source path and destination path first to preserve order
            if path in self._pending_events:
                self._flush_event(path)
            if dest_path and dest_path in self._pending_events:
                self._flush_event(dest_path)

            event = FSEvent(
                event_type="moved",
                src_path=str(path),
                dest_path=str(dest_path) if dest_path else "",
                is_directory=is_dir
            )
            self._emit_event(event)
            return

        # 3. Cancel active timer for the path
        if path in self._pending_timers:
            self._pending_timers[path].cancel()
            self._pending_timers.pop(path, None)

        # 4. Debounce state-machine logic
        old_state = self._pending_events.get(path)
        if old_state:
            if old_state.event_type == "created" and event_type == "modified":
                new_type = "created"
            elif old_state.event_type == "created" and event_type == "deleted":
                # Created then deleted immediately -> no event
                self._pending_events.pop(path, None)
                return
            elif old_state.event_type == "deleted" and event_type == "created":
                new_type = "modified"
            else:
                new_type = event_type
        else:
            new_type = event_type

        self._pending_events[path] = PendingState(event_type=new_type, is_directory=is_dir)

        # 5. Schedule flush
        if self.debounce > 0:
            self._pending_timers[path] = self._loop.call_later(self.debounce, self._flush_event, path)
        else:
            self._flush_event(path)

    def _flush_event(self, path: Path):
        state = self._pending_events.pop(path, None)
        self._pending_timers.pop(path, None)
        if state:
            event = FSEvent(
                event_type=state.event_type,
                src_path=str(path),
                is_directory=state.is_directory
            )
            self._emit_event(event)

    def _emit_event(self, event: FSEvent):
        if self._queue:
            self._queue.put_nowait(event)


def watch(path: Union[str, Path, List[Union[str, Path]]], on_change: Callable[[FSEvent], None], 
          recursive: bool = True, debounce: float = 0.05, 
          use_gitignore: bool = True, ignore_patterns: List[str] = None):
    """Level 2 Callback One-liner API."""
    w = FSWatcher(path, recursive=recursive, debounce=debounce, 
                  use_gitignore=use_gitignore, ignore_patterns=ignore_patterns)
    
    @w.on('*')
    def handler(event: FSEvent):
        on_change(event)
        
    w.start()
