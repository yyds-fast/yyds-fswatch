# -*- coding: utf-8 -*-

import asyncio
from pathlib import Path
from typing import List, Callable

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

    def _should_recurse_into(self, path: Path, resolved: bool = False) -> bool:
        """Whether subdirectories under ``path`` should be watched recursively."""
        if not self.recursive or not self._recursive_roots:
            return False
        target = path if resolved else path.resolve()
        for root in self._recursive_roots:
            if target == root:
                return True
            try:
                target.relative_to(root)
                return True
            except ValueError:
                continue
        return False

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
