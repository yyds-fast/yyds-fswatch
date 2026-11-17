# -*- coding: utf-8 -*-

import asyncio
import inspect
import logging
import queue
import threading
import warnings
from collections import OrderedDict
from pathlib import Path
from dataclasses import dataclass
from typing import Union, List, Sequence, Callable, Dict, Optional, Set

from .events import FSEvent
from .ignore import GitIgnoreFilter, canonicalize_watch_path

_STOP_SENTINEL = object()
_LOGGER = logging.getLogger(__name__)

@dataclass
class PendingState:
    event_type: str
    is_directory: bool

class FSWatcher:
    def __init__(self, path: Union[str, Path, Sequence[Union[str, Path]]],
                 recursive: bool = True, debounce: float = 0.05, 
                 use_gitignore: bool = True, ignore_patterns: List[str] = None,
                 max_queue_size: int = 4096, recovery_interval: float = 2.0,
                 poll_interval: float = 1.0, backend: str = "auto",
                 target_type: str = "auto", ignore_common_dirs: bool = True):
        """Create a file-system watcher.

        ``target_type`` may be ``"auto"``, ``"file"`` or ``"directory"``.
        The explicit forms are useful when the target does not exist yet.
        ``max_queue_size=0`` restores the legacy unbounded queue behavior.
        """
        if debounce < 0:
            raise ValueError("debounce must be greater than or equal to 0")
        if max_queue_size < 0:
            raise ValueError("max_queue_size must be greater than or equal to 0")
        if recovery_interval <= 0:
            raise ValueError("recovery_interval must be greater than 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than 0")
        if backend not in {"auto", "native", "polling"}:
            raise ValueError("backend must be 'auto', 'native', or 'polling'")
        if target_type not in {"auto", "file", "directory"}:
            raise ValueError("target_type must be 'auto', 'file', or 'directory'")

        self._file_targets: Set[Path] = set()
        self._directory_targets: Set[Path] = set()
        self._file_target_present: Dict[Path, bool] = {}
        self.paths: List[Path] = []

        if isinstance(path, (str, Path)):
            raw_paths = [Path(path)]
        else:
            raw_paths = [Path(p) for p in path]

        if not raw_paths:
            raise ValueError("at least one watch path is required")

        for p in raw_paths:
            p_resolved = p.resolve()
            is_file_target = target_type == "file" or (
                target_type == "auto" and p_resolved.is_file()
            )
            if is_file_target:
                self._file_targets.add(p_resolved)
                self._file_target_present[p_resolved] = p_resolved.exists()
                self.paths.append(p_resolved.parent)
            else:
                self._directory_targets.add(p_resolved)
                self.paths.append(p_resolved)

        self.paths = list(dict.fromkeys(self.paths))
        self._all_targets = frozenset(self._file_targets | self._directory_targets)
        # Kept for compatibility with code that inspected the old private flag.
        self._is_file_watch = bool(self._file_targets) and not self._directory_targets

        self.recursive = recursive
        self.debounce = debounce
        self.use_gitignore = use_gitignore
        self.ignore_patterns = ignore_patterns
        self.ignore_common_dirs = ignore_common_dirs
        self.max_queue_size = max_queue_size
        self.recovery_interval = recovery_interval
        self.poll_interval = poll_interval
        self.backend_preference = backend

        self._callbacks: Dict[str, List[Callable]] = {}
        self._queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._watcher = None
        self._ignore_filter = None
        self._backend_name = None
        self._recovery_task = None
        self._stop_monitor_task = None

        # Debounce & file presence state
        self._pending_events: Dict[Path, PendingState] = {}
        self._pending_timers: Dict[Path, asyncio.TimerHandle] = {}
        self._observed_files: OrderedDict = OrderedDict()
        self._max_observed_files = 8192

        # Sync iteration state
        self._sync_queue: Optional[queue.Queue] = None
        self._bg_thread: Optional[threading.Thread] = None
        self._bg_loop: Optional[asyncio.AbstractEventLoop] = None
        self._bg_task: Optional[asyncio.Task] = None
        self._ready_event = threading.Event()
        self._stop_requested = threading.Event()
        self._sync_overflow_lock = threading.Lock()
        self._startup_error = None
        self._queue_overflow_pending = False
        self._sync_overflow_pending = False
        self._dropped_event_count = 0
        self.is_ready = False

    @property
    def backend(self) -> Optional[str]:
        """Name of the active backend, once the watcher has started."""
        return self._backend_name

    @property
    def is_running(self) -> bool:
        """Whether the context is active and its platform backend is healthy."""
        return bool(
            self.is_ready and self._watcher and
            getattr(self._watcher, "is_running", False)
        )

    @property
    def dropped_event_count(self) -> int:
        """Number of userspace events dropped because a queue was full."""
        return self._dropped_event_count

    def wait_until_ready(self, timeout: Optional[float] = None) -> bool:
        """Wait for a background/synchronous watcher to finish starting."""
        ready = self._ready_event.wait(timeout)
        if ready and self._startup_error is not None:
            raise RuntimeError("file-system watcher failed to start") from self._startup_error
        return ready

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
        if self.is_ready or self._loop is not None:
            raise RuntimeError("watcher is already running")
        self._ready_event.clear()
        self._startup_error = None
        try:
            asyncio.run(self._run_decorator_loop())
        except KeyboardInterrupt:
            self.stop()
        finally:
            self._stop_requested.clear()

    def stop(self):
        """Request a running watcher to stop.

        This method is idempotent and safe to call from another thread. Context
        managers still perform the actual resource cleanup.
        """
        self._stop_requested.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._request_async_stop)
            except RuntimeError:
                pass

    close = stop

    def _request_async_stop(self):
        if self._queue is not None:
            self._force_async_put(_STOP_SENTINEL)

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
                            result = cb(event)
                            if inspect.isawaitable(result):
                                await result
                    except Exception:
                        _LOGGER.exception("Error in yyds-fswatch callback")

    # --- Synchronous Context & Iterator Mode (Level 3) ---
    def __enter__(self):
        if self._bg_thread is not None and self._bg_thread.is_alive():
            raise RuntimeError("watcher is already running")
        self._ready_event.clear()
        self._startup_error = None
        self._sync_queue = queue.Queue(maxsize=self.max_queue_size)
        self._bg_thread = threading.Thread(target=self._run_bg_loop, name="yyds-fswatch")
        self._bg_thread.daemon = True
        self._bg_thread.start()
        try:
            ready = self.wait_until_ready(timeout=5.0)
        except Exception:
            self._cancel_bg_thread()
            self._bg_thread.join(timeout=2.0)
            if not self._bg_thread.is_alive():
                self._bg_thread = None
            self._stop_requested.clear()
            raise
        if not ready:
            self._cancel_bg_thread()
            self._bg_thread.join(timeout=2.0)
            if not self._bg_thread.is_alive():
                self._bg_thread = None
            self._stop_requested.clear()
            raise TimeoutError("file-system watcher did not become ready within 5 seconds")
        return self

    def _cancel_bg_thread(self):
        self.stop()
        bg_loop = self._bg_loop
        if bg_loop is not None and bg_loop.is_running():
            try:
                bg_loop.call_soon_threadsafe(self._stop_bg_loop)
            except RuntimeError:
                pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._cancel_bg_thread()
        if self._bg_thread:
            self._bg_thread.join(timeout=2.0)
            if not self._bg_thread.is_alive():
                self._bg_thread = None
        # Put final sentinel just in case.
        if self._sync_queue is not None:
            self._force_sync_put(_STOP_SENTINEL)
        self._stop_requested.clear()

    def _run_bg_loop(self):
        self._bg_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._bg_loop)
        
        async def run():
            try:
                async with self as w:
                    async for event in w:
                        self._put_sync_event(event)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._startup_error = exc
                self._ready_event.set()
            finally:
                if self._sync_queue is not None:
                    self._force_sync_put(_STOP_SENTINEL)

        self._bg_task = self._bg_loop.create_task(run())
        try:
            self._bg_loop.run_until_complete(self._bg_task)
        finally:
            self._bg_loop.close()
            self._bg_loop = None

    def _stop_bg_loop(self):
        if self._bg_task:
            self._bg_task.cancel()

    def __iter__(self):
        return self

    def __next__(self):
        if self._sync_queue is None:
            raise RuntimeError("use 'with FSWatcher(...) as watcher' before iterating")
        event = self._sync_queue.get()
        if event is _STOP_SENTINEL:
            if self._startup_error is not None:
                raise RuntimeError("file-system watcher stopped after an error") from self._startup_error
            raise StopIteration
        if isinstance(event, FSEvent) and event.event_type == "overflow":
            with self._sync_overflow_lock:
                self._sync_overflow_pending = False
        return event

    # --- Asynchronous Context & Iterator Mode (Level 4) ---
    async def __aenter__(self):
        if self.is_ready or self._watcher is not None:
            raise RuntimeError("watcher is already running")
        self._ready_event.clear()
        self._startup_error = None
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.max_queue_size)
        self._recovery_task = None
        self._stop_monitor_task = None
        self._queue_overflow_pending = False
        with self._sync_overflow_lock:
            self._sync_overflow_pending = False
        self._observed_files.clear()
        self._file_target_present = {p: p.exists() for p in self._file_targets}

        try:
            # Load Ignore rules
            self._ignore_filter = GitIgnoreFilter(
                self.paths,
                self.use_gitignore,
                self.ignore_patterns,
                ignore_common_dirs=self.ignore_common_dirs,
            )

            # Factory resolve platform watcher
            from ._platforms import get_platform_watcher_class
            watcher_cls = get_platform_watcher_class(self.backend_preference)
            self._watcher = self._create_platform_watcher(watcher_cls)

            if self._existing_paths():
                await self._start_platform_watcher()

            self.is_ready = True
            self._ready_event.set()
            self._recovery_task = self._loop.create_task(self._recovery_loop())
            self._stop_monitor_task = self._loop.create_task(self._stop_monitor_loop())
            if self._stop_requested.is_set():
                self._request_async_stop()
            return self
        except Exception as exc:
            self._startup_error = exc
            self._ready_event.set()
            if self._watcher is not None:
                try:
                    await self._stop_platform_watcher()
                except Exception:
                    pass
                self._watcher = None
            if self._ignore_filter is not None:
                self._ignore_filter.close()
                self._ignore_filter = None
            self._loop = None
            self._stop_requested.clear()
            raise

    def _existing_paths(self):
        return frozenset(p for p in self.paths if p.exists())

    def _create_platform_watcher(self, watcher_cls):
        effective_recursive = self.recursive and bool(self._directory_targets)
        kwargs = dict(
            paths=self.paths,
            recursive=effective_recursive,
            loop=self._loop,
            on_raw_event=self._on_raw_event,
            ignore_filter=self._ignore_filter,
        )
        from ._platforms._polling import PollingWatcher
        if watcher_cls is PollingWatcher:
            kwargs["interval"] = self.poll_interval
        if effective_recursive and self._file_targets:
            try:
                if "recursive_roots" in inspect.signature(watcher_cls.__init__).parameters:
                    kwargs["recursive_roots"] = self._directory_targets
            except (TypeError, ValueError):
                pass
        watcher = watcher_cls(**kwargs)
        self._backend_name = watcher_cls.__name__
        return watcher

    async def _start_platform_watcher(self):
        try:
            await self._watcher.astart()
        except Exception as exc:
            from ._platforms._polling import PollingWatcher
            if self.backend_preference != "auto" or isinstance(self._watcher, PollingWatcher):
                raise
            try:
                await self._watcher.aclose()
            except Exception:
                pass
            warnings.warn(
                "Native file-system watcher failed to start; falling back to polling: "
                f"{exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            self._watcher = self._create_platform_watcher(PollingWatcher)
            await self._watcher.astart()

    async def _stop_platform_watcher(self):
        """Stop a backend without blocking the caller's asyncio loop."""
        if self._watcher is not None:
            await self._watcher.aclose()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.is_ready = False

        background_tasks = [self._recovery_task, self._stop_monitor_task]
        for task in background_tasks:
            if task:
                task.cancel()
        for task in background_tasks:
            if not task:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._recovery_task = None
        self._stop_monitor_task = None

        # Stop background platform monitoring
        for timer in self._pending_timers.values():
            timer.cancel()
        self._pending_timers.clear()
        self._pending_events.clear()
        self._observed_files.clear()

        if self._watcher:
            try:
                await self._stop_platform_watcher()
            except Exception as exc:
                if self._loop is not None:
                    self._loop.call_exception_handler({
                        "message": "yyds-fswatch backend cleanup failed",
                        "exception": exc,
                    })
            self._watcher = None
            # Let cancelled backend tasks observe cancellation before a
            # short-lived background event loop is closed.
            await asyncio.sleep(0)

        if self._ignore_filter:
            self._ignore_filter.close()
            self._ignore_filter = None

        if self._queue is not None:
            self._force_async_put(_STOP_SENTINEL)
        self._loop = None
        self._stop_requested.clear()

    async def _stop_monitor_loop(self):
        """Fallback for event loops whose cross-thread wakeup is delayed."""
        while self.is_ready and not self._stop_requested.is_set():
            await asyncio.sleep(0.1)
        if self.is_ready and self._stop_requested.is_set():
            self._request_async_stop()

    async def _recovery_loop(self):
        existing_paths = self._existing_paths()
        watcher_running = bool(existing_paths)
        
        while self.is_ready:
            try:
                await asyncio.sleep(self.recovery_interval)
                if not self.is_ready:
                    break
                current_paths = self._existing_paths()

                backend_healthy = bool(
                    self._watcher and self._watcher.is_healthy_for(current_paths)
                )
                restarted_for_health = False
                if current_paths and watcher_running and not backend_healthy:
                    try:
                        await self._stop_platform_watcher()
                    except Exception:
                        pass
                    watcher_running = False
                    restarted_for_health = True

                if current_paths != existing_paths and watcher_running:
                    if self._watcher:
                        await self._stop_platform_watcher()
                    watcher_running = False

                if (current_paths != existing_paths or restarted_for_health) and self._ignore_filter:
                    self._ignore_filter.refresh()

                if current_paths and not watcher_running:
                    if self._watcher:
                        try:
                            await self._start_platform_watcher()
                            watcher_running = True
                        except Exception as exc:
                            self._loop.call_exception_handler({
                                "message": "yyds-fswatch failed to recover its backend",
                                "exception": exc,
                            })
                existing_paths = current_paths
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._loop.call_exception_handler({
                    "message": "yyds-fswatch recovery loop failed",
                    "exception": exc,
                })

    def __aiter__(self):
        return self

    async def __anext__(self) -> FSEvent:
        if self._queue is None:
            raise RuntimeError("use 'async with FSWatcher(...) as watcher' before iterating")
        event = await self._queue.get()
        if event is _STOP_SENTINEL:
            raise StopAsyncIteration
        if isinstance(event, FSEvent) and event.event_type == "overflow":
            self._queue_overflow_pending = False
        return event

    # --- Debounce Mechanism ---
    def _on_raw_event(self, event_type: str, path: Path, is_dir: bool, dest_path: Path = None):
        """Called by the platform watcher when a change is detected."""
        if self._loop is None or self._loop.is_closed():
            return
        try:
            if asyncio.get_running_loop() is self._loop:
                # Linux's add_reader callback and PollingWatcher's diff both
                # already execute on the target loop. Avoid the lock and
                # self-pipe write performed by call_soon_threadsafe for every
                # event in those hot paths.
                self._push_to_debounce(event_type, path, is_dir, dest_path)
                return
        except RuntimeError:
            # Native Windows/macOS callbacks run in helper threads without a
            # running asyncio loop and must take the thread-safe path below.
            pass
        self._loop.call_soon_threadsafe(self._push_to_debounce, event_type, path, is_dir, dest_path)

    def _canonicalize_path(self, path: Path) -> Path:
        return canonicalize_watch_path(path, self._all_targets)

    def _remember_observed_file(self, path: Path):
        if path in self._observed_files:
            self._observed_files.move_to_end(path)
        else:
            self._observed_files[path] = True
            if len(self._observed_files) > self._max_observed_files:
                self._observed_files.popitem(last=False)

    def _forget_observed_path(self, path: Path, is_dir: bool):
        self._observed_files.pop(path, None)
        if is_dir and self._observed_files:
            to_remove = []
            for observed in self._observed_files:
                try:
                    observed.relative_to(path)
                    to_remove.append(observed)
                except ValueError:
                    continue
            for observed in to_remove:
                self._observed_files.pop(observed, None)

    def _remap_observed_subtree(self, old_dir: Path, new_dir: Path):
        if not self._observed_files:
            return
        remapped = []
        for observed in list(self._observed_files):
            try:
                relative = observed.relative_to(old_dir)
            except ValueError:
                continue
            self._observed_files.pop(observed, None)
            remapped.append(new_dir / relative)
        for new_path in remapped:
            self._remember_observed_file(new_path)

    def _push_to_debounce(self, event_type: str, path: Path, is_dir: bool, dest_path: Path = None):
        if not self.is_ready or self._stop_requested.is_set():
            return
        if event_type == "overflow":
            # An overflow means changes to nested .gitignore files may have
            # been missed. Drop rule state before the backend reconciles its
            # watches so subsequent visibility checks reload current rules.
            if self._ignore_filter:
                self._ignore_filter.refresh()
            self._observed_files.clear()
            # Flush all pending events first to preserve order
            for p in list(self._pending_events.keys()):
                self._flush_event(p)
            
            # Emit overflow event immediately
            event = FSEvent(
                event_type="overflow",
                src_path=str(path),
                is_directory=False
            )
            self._emit_event(event)
            return

        # Watch targets are canonicalized at construction. Canonicalize parent
        # directories so macOS aliases such as /var and /private/var remain
        # in-scope without resolving symlinks inside watched roots away.
        path = self._canonicalize_path(path)
        dest_path = self._canonicalize_path(dest_path) if dest_path else None

        if self._ignore_filter:
            self._ignore_filter.notify_event(event_type, path, is_dir, dest_path)

        source_visible = self._is_visible_path(path, is_dir, canonicalized=True)
        dest_visible = bool(
            dest_path and self._is_visible_path(dest_path, is_dir, canonicalized=True)
        )

        # Convert moves across the visible boundary into a deletion or creation.
        if event_type == "moved":
            if source_visible and dest_visible:
                pass
            elif source_visible:
                event_type = "deleted"
                dest_path = None
            elif dest_visible:
                if dest_path in self._file_targets:
                    was_present = self._file_target_present.get(dest_path, False)
                else:
                    pending_state = self._pending_events.get(dest_path)
                    was_present = (
                        dest_path in self._observed_files or
                        (pending_state is not None and pending_state.event_type != "deleted")
                    )
                event_type = "modified" if was_present else "created"
                path = dest_path
                dest_path = None
            else:
                return
        elif not source_visible:
            return

        # FSEvents and ReadDirectoryChangesW can surface an atomic replacement
        # as a plain creation of an already watched file. Preserve the public
        # replacement semantics by reporting it as a modification.
        if (
            event_type == "created" and
            (
                (path in self._file_targets and self._file_target_present.get(path, False)) or
                (not is_dir and path in self._observed_files)
            )
        ):
            event_type = "modified"

        self._update_file_target_state(event_type, path, dest_path)
        if event_type in {"created", "modified"}:
            if not is_dir:
                self._remember_observed_file(path)
        elif event_type == "deleted":
            self._forget_observed_path(path, is_dir)
        elif event_type == "moved":
            if is_dir and dest_path is not None:
                self._remap_observed_subtree(path, dest_path)
            else:
                self._forget_observed_path(path, is_dir)
                if not is_dir and dest_path is not None:
                    self._remember_observed_file(dest_path)

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

    def _is_visible_path(self, path: Path, is_dir: bool, canonicalized: bool = False) -> bool:
        if not self._is_path_in_scope(path, canonicalized=canonicalized):
            return False
        if self._ignore_filter and self._ignore_filter.is_ignored(path, is_dir):
            return False
        return True

    def _is_path_in_scope(self, path: Path, canonicalized: bool = False) -> bool:
        if not canonicalized:
            path = self._canonicalize_path(path)
        if path in self._file_targets:
            return True
        for root in self._directory_targets:
            if path == root:
                return True
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if self.recursive or len(relative.parts) == 1:
                return True
        return False

    def _update_file_target_state(self, event_type: str, path: Path, dest_path: Path = None):
        if path in self._file_targets:
            if event_type in {"created", "modified"}:
                self._file_target_present[path] = True
            elif event_type in {"deleted", "moved"}:
                self._file_target_present[path] = False
        if event_type == "moved" and dest_path in self._file_targets:
            self._file_target_present[dest_path] = True

    def _flush_event(self, path: Path):
        state = self._pending_events.pop(path, None)
        timer = self._pending_timers.pop(path, None)
        if timer is not None:
            timer.cancel()
        if state:
            event = FSEvent(
                event_type=state.event_type,
                src_path=str(path),
                is_directory=state.is_directory
            )
            self._emit_event(event)

    def _emit_event(self, event: FSEvent):
        if self._queue is None or self._stop_requested.is_set():
            return
        try:
            self._queue.put_nowait(event)
            if event.event_type != "overflow":
                self._queue_overflow_pending = False
        except asyncio.QueueFull:
            self._dropped_event_count += 1
            if self._queue_overflow_pending:
                return
            self._queue_overflow_pending = True
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._force_async_put(self._make_backpressure_overflow_event())

    def _make_backpressure_overflow_event(self):
        root = self.paths[0] if self.paths else Path(".").resolve()
        return FSEvent("overflow", str(root), is_directory=False)

    def _force_async_put(self, item):
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(item)

    def _put_sync_event(self, event):
        if self._sync_queue is None or self._stop_requested.is_set():
            return
        with self._sync_overflow_lock:
            try:
                self._sync_queue.put_nowait(event)
                if isinstance(event, FSEvent) and event.event_type != "overflow":
                    self._sync_overflow_pending = False
            except queue.Full:
                self._dropped_event_count += 1
                if self._sync_overflow_pending:
                    return
                self._sync_overflow_pending = True
                try:
                    self._sync_queue.get_nowait()
                except queue.Empty:
                    pass
                self._force_sync_put(self._make_backpressure_overflow_event())

    def _force_sync_put(self, item):
        if self._sync_queue is None:
            return
        try:
            self._sync_queue.put_nowait(item)
        except queue.Full:
            try:
                self._sync_queue.get_nowait()
            except queue.Empty:
                pass
            self._sync_queue.put_nowait(item)


def watch(path: Union[str, Path, Sequence[Union[str, Path]]], on_change: Callable[[FSEvent], None],
          recursive: bool = True, debounce: float = 0.05, 
          use_gitignore: bool = True, ignore_patterns: List[str] = None,
          max_queue_size: int = 4096, recovery_interval: float = 2.0,
          poll_interval: float = 1.0, backend: str = "auto",
          target_type: str = "auto", ignore_common_dirs: bool = True):
    """Level 2 Callback One-liner API."""
    w = FSWatcher(path, recursive=recursive, debounce=debounce, 
                  use_gitignore=use_gitignore, ignore_patterns=ignore_patterns,
                  max_queue_size=max_queue_size,
                  recovery_interval=recovery_interval,
                  poll_interval=poll_interval, backend=backend,
                  target_type=target_type,
                  ignore_common_dirs=ignore_common_dirs)
    
    @w.on('*')
    def handler(event: FSEvent):
        return on_change(event)
        
    w.start()
    return w
