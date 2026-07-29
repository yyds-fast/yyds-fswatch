# -*- coding: utf-8 -*-

import os
import asyncio
import threading
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
        self._close_done = None
        self._close_error = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def _begin_start(self):
        if self._running:
            raise RuntimeError("Polling watcher is already running")
        if self._close_done is not None:
            if not self._close_done.is_set():
                raise RuntimeError("Polling watcher is still shutting down")
            if self._close_error is not None:
                raise RuntimeError("Polling watcher failed to shut down") from self._close_error
            self._close_done = None
        self._running = True
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="yyds-fswatch-polling",
        )

    def _abort_start(self):
        self._running = False
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=True)
        self._snapshot.clear()

    def start(self):
        """Synchronously initialize polling for direct backend users."""
        self._begin_start()
        try:
            self._snapshot = self._scan()
            self._task = self.loop.create_task(self._poll_loop())
        except Exception:
            self._abort_start()
            raise

    async def astart(self):
        """Initialize the first snapshot without blocking the asyncio loop."""
        self._begin_start()
        try:
            self._snapshot = await self._scan_in_executor()
            if self._running:
                self._task = self.loop.create_task(self._poll_loop())
        except BaseException:
            await self.aclose()
            raise

    def stop(self):
        """Request shutdown without blocking the event loop.

        ``aclose`` performs the asynchronous worker join. Keeping this method
        non-blocking also lets recovery stop a backend promptly before it is
        restarted.
        """
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def aclose(self):
        """Stop polling and join an in-flight scan without blocking asyncio."""
        self.stop()

        if self._close_done is None:
            executor = self._executor
            self._executor = None
            if executor is not None:
                # Waiting is necessary because a running scan may still read
                # the shared ignore filter. Use a dedicated join thread rather
                # than the loop's default executor: the latter must itself be
                # synchronously joined when an asyncio loop is closed.
                self._close_done = threading.Event()

                def join_executor():
                    try:
                        executor.shutdown(wait=True)
                    except BaseException as exc:
                        self._close_error = exc
                    finally:
                        self._close_done.set()

                thread = threading.Thread(
                    target=join_executor,
                    name="yyds-fswatch-polling-close",
                    daemon=True,
                )
                thread.start()

        if self._close_done is not None:
            # Use the same heartbeat as polling itself. Some embedded event
            # loops delay selector wakeups from worker threads, so waiting on
            # the notification without a timer can postpone cleanup.
            heartbeat = min(self.interval, 0.1)
            while not self._close_done.is_set():
                await asyncio.sleep(heartbeat)
            error = self._close_error
            self._close_done = None
            self._close_error = None
            if error is not None:
                raise RuntimeError("Polling watcher failed to shut down") from error
        self._snapshot.clear()

    async def _scan_in_executor(self):
        if self._executor is None:
            raise RuntimeError("Polling watcher is not running")
        scan_future = self.loop.run_in_executor(self._executor, self._scan)
        # Some embedded/sandboxed event loops do not wake their selector
        # promptly after a worker performs file-system I/O. A bounded heartbeat
        # keeps startup, cancellation, and periodic scans responsive.
        heartbeat = min(self.interval, 0.1)
        while not scan_future.done():
            await asyncio.sleep(heartbeat)
        return await scan_future

    async def _poll_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                # Directory scans are blocking I/O. Keep them off the caller's
                # asyncio loop so a slow network mount cannot stall the app.
                new_snapshot = await self._scan_in_executor()
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
        for path, state in sorted(created.items(), key=lambda item: str(item[0])):
            identity = self._identity(state)
            if identity is not None:
                created_by_identity.setdefault(identity, []).append(path)

        move_pairs = []
        consumed_deleted = set()
        consumed_created = set()
        for old_path, old_state in sorted(deleted.items(), key=lambda item: str(item[0])):
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
        for old_path, new_path, is_dir in sorted(
            move_pairs,
            key=lambda item: (len(item[0].parts), str(item[0]), str(item[1])),
        ):
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

        for path, state in sorted(
            deleted.items(),
            key=lambda item: (-len(item[0].parts), str(item[0])),
        ):
            if path not in consumed_deleted:
                self.on_raw_event("deleted", path, state[2])

        for path, state in sorted(
            created.items(),
            key=lambda item: (len(item[0].parts), str(item[0])),
        ):
            if path not in consumed_created:
                self.on_raw_event("created", path, state[2])

        for path in sorted(old.keys() & new.keys(), key=str):
            if old[path] != new[path]:
                self.on_raw_event("modified", path, new[path][2])
