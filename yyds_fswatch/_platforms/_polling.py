# -*- coding: utf-8 -*-

import os
import asyncio
from pathlib import Path
from typing import List, Callable, Dict, Tuple

from ._base import BaseWatcher

class PollingWatcher(BaseWatcher):
    def __init__(self, paths: List[Path], recursive: bool, loop: asyncio.AbstractEventLoop, 
                 on_raw_event: Callable, ignore_filter=None, interval: float = 1.0):
        super().__init__(paths, recursive, loop, on_raw_event)
        self._ignore_filter = ignore_filter
        self.interval = interval
        self._snapshot: Dict[Path, Tuple[float, int, bool]] = {}
        self._task: asyncio.Task = None
        self._running = False

    def start(self):
        self._running = True
        self._snapshot = self._scan()
        self._task = self.loop.create_task(self._poll_loop())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self._snapshot.clear()

    async def _poll_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                new_snapshot = self._scan()
                self._diff(self._snapshot, new_snapshot)
                self._snapshot = new_snapshot
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _scan(self) -> Dict[Path, Tuple[float, int, bool]]:
        snapshot = {}
        for p in self.paths:
            if not p.exists():
                continue
            self._scan_path(p, snapshot)
        return snapshot

    def _scan_path(self, path: Path, snapshot: dict):
        try:
            stat = path.stat()
            is_dir = path.is_dir()
            snapshot[path] = (stat.st_mtime, stat.st_size, is_dir)
        except OSError:
            return

        if is_dir and (self.recursive or path in self.paths):
            try:
                for entry in os.scandir(path):
                    entry_path = Path(entry.path)
                    
                    # Apply ignore filter
                    if self._ignore_filter and self._ignore_filter.is_ignored(entry_path, is_dir=entry.is_dir()):
                        continue

                    if entry.is_dir():
                        if self.recursive:
                            self._scan_path(entry_path, snapshot)
                        else:
                            try:
                                stat = entry.stat()
                                snapshot[entry_path] = (stat.st_mtime, stat.st_size, True)
                            except OSError:
                                pass
                    else:
                        try:
                            stat = entry.stat()
                            snapshot[entry_path] = (stat.st_mtime, stat.st_size, False)
                        except OSError:
                            pass
            except OSError:
                pass

    def _diff(self, old: Dict[Path, Tuple[float, int, bool]], new: Dict[Path, Tuple[float, int, bool]]):
        # 1. Detect deleted paths
        for path, (mtime, size, is_dir) in old.items():
            if path not in new:
                self.on_raw_event("deleted", path, is_dir)

        # 2. Detect created and modified paths
        for path, (new_mtime, new_size, is_dir) in new.items():
            if path not in old:
                self.on_raw_event("created", path, is_dir)
            else:
                old_mtime, old_size, old_is_dir = old[path]
                if new_mtime != old_mtime or new_size != old_size or is_dir != old_is_dir:
                    self.on_raw_event("modified", path, is_dir)
