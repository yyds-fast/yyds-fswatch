# -*- coding: utf-8 -*-

import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Callable, Dict, Tuple

from ._base import BaseWatcher

class PollingWatcher(BaseWatcher):
    def __init__(self, paths: List[Path], recursive: bool, loop: asyncio.AbstractEventLoop, 
                 on_raw_event: Callable, ignore_filter=None, interval: float = 1.0):
        super().__init__(paths, recursive, loop, on_raw_event)
        self._ignore_filter = ignore_filter
        self.interval = interval
        self._snapshot: Dict[Path, Tuple[int, int, bool, int, int]] = {}
        self._task: asyncio.Task = None
        self._executor = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def start(self):
        if self._running:
            raise RuntimeError("Polling watcher is already running")
        self._running = True
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="yyds-fswatch-polling",
        )
        try:
            self._snapshot = self._scan()
            self._task = self.loop.create_task(self._poll_loop())
        except Exception:
            self._running = False
            self._executor.shutdown(wait=True)
            self._executor = None
            raise

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._executor is not None:
            # Wait for a scan already executing in the worker before the
            # ignore filter and snapshot are released by FSWatcher.
            self._executor.shutdown(wait=True)
            self._executor = None
        self._snapshot.clear()

    async def _poll_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                # Directory scans are blocking I/O. Keep them off the caller's
                # asyncio loop so a slow network mount cannot stall the app.
                scan_future = self.loop.run_in_executor(self._executor, self._scan)
                # Some embedded/sandboxed event loops do not wake their
                # selector promptly after a worker performs file-system I/O.
                # A bounded heartbeat also makes cancellation responsive while
                # a slow mount is being scanned.
                heartbeat = min(self.interval, 0.1)
                while not scan_future.done():
                    await asyncio.sleep(heartbeat)
                new_snapshot = await scan_future
                self._diff(self._snapshot, new_snapshot)
                self._snapshot = new_snapshot
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.loop.call_exception_handler({
                    "message": "yyds-fswatch polling scan failed",
                    "exception": exc,
                })

    def _scan(self) -> Dict[Path, Tuple[int, int, bool, int, int]]:
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
            snapshot[path] = (stat.st_mtime_ns, stat.st_size, is_dir, stat.st_dev, stat.st_ino)
        except OSError:
            return

        if is_dir and (self.recursive or path in self.paths):
            try:
                for entry in os.scandir(path):
                    entry_path = Path(entry.path)
                    entry_is_dir = entry.is_dir(follow_symlinks=False)
                    
                    # Apply ignore filter
                    if self._ignore_filter and self._ignore_filter.is_ignored(entry_path, is_dir=entry_is_dir):
                        continue

                    if entry_is_dir:
                        if self.recursive:
                            self._scan_path(entry_path, snapshot)
                        else:
                            try:
                                stat = entry.stat(follow_symlinks=False)
                                snapshot[entry_path] = (
                                    stat.st_mtime_ns, stat.st_size, True,
                                    stat.st_dev, stat.st_ino,
                                )
                            except OSError:
                                pass
                    else:
                        try:
                            stat = entry.stat(follow_symlinks=False)
                            snapshot[entry_path] = (
                                stat.st_mtime_ns, stat.st_size, False,
                                stat.st_dev, stat.st_ino,
                            )
                        except OSError:
                            pass
            except OSError:
                pass

    @staticmethod
    def _identity(state):
        # Some network/virtual file systems report inode 0 for every entry;
        # those values cannot safely be used to infer a rename.
        if not state[4]:
            return None
        return state[3], state[4], state[2]

    def _diff(self, old: Dict[Path, Tuple[int, int, bool, int, int]],
              new: Dict[Path, Tuple[int, int, bool, int, int]]):
        deleted = {path: state for path, state in old.items() if path not in new}
        created = {path: state for path, state in new.items() if path not in old}

        created_by_identity = {}
        for path, state in created.items():
            identity = self._identity(state)
            if identity is not None:
                created_by_identity.setdefault(identity, []).append(path)

        move_pairs = []
        consumed_deleted = set()
        consumed_created = set()
        for old_path, old_state in deleted.items():
            identity = self._identity(old_state)
            if identity is None:
                continue
            matches = created_by_identity.get(identity, [])
            new_path = next((p for p in matches if p not in consumed_created), None)
            if new_path is None:
                continue
            consumed_deleted.add(old_path)
            consumed_created.add(new_path)
            move_pairs.append((old_path, new_path, old_state[2]))

        # A directory move also changes every descendant path. Emit one event
        # for the top-level move, not one synthetic move per child.
        emitted_directory_moves = []
        for old_path, new_path, is_dir in sorted(move_pairs, key=lambda item: len(item[0].parts)):
            covered = False
            for old_parent, new_parent in emitted_directory_moves:
                try:
                    relative = old_path.relative_to(old_parent)
                except ValueError:
                    continue
                if new_path == new_parent / relative:
                    covered = True
                    break
            if covered:
                continue
            self.on_raw_event("moved", old_path, is_dir, dest_path=new_path)
            if is_dir:
                emitted_directory_moves.append((old_path, new_path))

        for path, state in deleted.items():
            if path not in consumed_deleted:
                self.on_raw_event("deleted", path, state[2])

        for path, state in created.items():
            if path not in consumed_created:
                self.on_raw_event("created", path, state[2])

        for path in old.keys() & new.keys():
            if old[path] != new[path]:
                self.on_raw_event("modified", path, new[path][2])
