# -*- coding: utf-8 -*-

import asyncio
import threading
from pathlib import Path
from typing import List, Callable

from ..ignore import path_prefix_pair, subtree_relative_suffix

class BaseWatcher:
    def __init__(self, paths: List[Path], recursive: bool, loop: asyncio.AbstractEventLoop,
                 on_raw_event: Callable, ignore_filter=None, recursive_roots=None):
        self.paths = paths
        self.recursive = recursive
        self.loop = loop
        self.on_raw_event = on_raw_event  # Signature: on_raw_event(event_type: str, path: Path, is_dir: bool, dest_path: Path = None, cookie: int = 0)
        self._ignore_filter = ignore_filter
        self._watched_roots = frozenset(p.resolve() for p in paths)
        if recursive_roots is not None:
            self._recursive_roots = frozenset(p.resolve() for p in recursive_roots)
        elif recursive:
            self._recursive_roots = self._watched_roots
        else:
            self._recursive_roots = frozenset()
        self._recursive_prefixes = tuple(
            path_prefix_pair(root) for root in self._recursive_roots
        )

    def _should_recurse_into(self, path: Path, resolved: bool = False) -> bool:
        """Whether subdirectories under ``path`` should be watched recursively."""
        if not self.recursive or not self._recursive_prefixes:
            return False
        target = path if resolved else path.resolve()
        for exact_norm, prefix_norm in self._recursive_prefixes:
            if subtree_relative_suffix(target, exact_norm, prefix_norm) is not None:
                return True
        return False

    async def _run_in_helper_thread(self, func: Callable, thread_name: str,
                                    heartbeat: float = 0.05):
        """Run a blocking backend lifecycle operation without stalling asyncio."""
        running_loop = asyncio.get_running_loop()
        done_flag = threading.Event()
        done_async = asyncio.Event()
        outcome = []

        def worker():
            try:
                outcome.append((True, func()))
            except BaseException as exc:
                outcome.append((False, exc))
            finally:
                done_flag.set()
                if not running_loop.is_closed():
                    try:
                        running_loop.call_soon_threadsafe(done_async.set)
                    except RuntimeError:
                        pass

        thread = threading.Thread(target=worker, name=thread_name, daemon=True)
        thread.start()
        while not done_flag.is_set():
            try:
                await asyncio.wait_for(done_async.wait(), timeout=heartbeat)
            except asyncio.TimeoutError:
                pass
        ok, value = outcome[0]
        if not ok:
            raise value
        return value

    def reconcile_watches(self):
        """Reconcile backend directory watches after ignore rules change."""
        return None

    def _should_skip_event(self, event_type: str, event_path: Path, is_dir: bool,
                           dest_path: Path = None) -> bool:
        """Filter ignored worker-thread events while keeping rule updates intact."""
        filt = self._ignore_filter
        if filt is None:
            return False
        ignore_fn = getattr(filt, "is_ignored", None)
        if ignore_fn is None:
            return False
        if event_path.name == ".gitignore" or (dest_path is not None and dest_path.name == ".gitignore"):
            filt.notify_event(event_type, event_path, is_dir, dest_path)
            return False
        if is_dir and event_type in {"deleted", "moved"}:
            filt.notify_event(event_type, event_path, is_dir, dest_path)
        try:
            if event_type == "moved" and dest_path is not None:
                return bool(ignore_fn(event_path, is_dir) and ignore_fn(dest_path, is_dir))
            return bool(ignore_fn(event_path, is_dir))
        except TypeError:
            return False

    def start(self):
        raise NotImplementedError

    async def astart(self):
        """Start the backend from an asyncio context.

        Native backends start synchronously by default. Backends with blocking
        initialization may override this method to keep the caller's loop
        responsive.
        """
        self.start()

    def stop(self):
        raise NotImplementedError

    async def aclose(self):
        """Asynchronously release backend resources.

        Native backends can normally stop synchronously. Backends that own
        blocking worker operations may override this method so callers do not
        block the asyncio loop while waiting for those workers to finish.
        """
        self.stop()

    @property
    def is_running(self) -> bool:
        return False

    def is_healthy_for(self, paths: List[Path]) -> bool:
        """Whether this backend still covers every currently existing root."""
        return self.is_running
