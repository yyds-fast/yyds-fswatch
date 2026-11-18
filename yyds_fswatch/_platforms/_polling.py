# -*- coding: utf-8 -*-

import os
import stat as stat_module
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Callable, Dict, Tuple

from ._base import BaseWatcher

class PollingWatcher(BaseWatcher):
    def __init__(self, paths: List[Path], recursive: bool, loop: asyncio.AbstractEventLoop, 
                 on_raw_event: Callable, ignore_filter=None, interval: float = 1.0,
                 recursive_roots=None):
        super().__init__(
            paths,
            recursive,
            loop,
            on_raw_event,
            ignore_filter=ignore_filter,
            recursive_roots=recursive_roots,
        )
        self.interval = interval
        self._snapshot: Dict[Path, Tuple[int, int, bool, int, int]] = {}
        self._task: asyncio.Task = None
        self._executor = None
        self._close_done = None
        self._close_async_event = None
        self._close_async_loop = None
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
                running_loop = asyncio.get_running_loop()
                done_async = asyncio.Event()
                self._close_async_event = done_async
                self._close_async_loop = running_loop

                def join_executor():
                    try:
                        executor.shutdown(wait=True)
                    except BaseException as exc:
                        self._close_error = exc
                    finally:
                        self._close_done.set()
                        if not running_loop.is_closed():
                            try:
                                running_loop.call_soon_threadsafe(done_async.set)
                            except RuntimeError:
                                pass

                thread = threading.Thread(
                    target=join_executor,
                    name="yyds-fswatch-polling-close",
                    daemon=True,
                )
                thread.start()

        if self._close_done is not None:
            # Wake up immediately via call_soon_threadsafe while preserving a
            # bounded heartbeat for embedded loops with delayed selector wakeups.
            heartbeat = min(self.interval, 0.1)
            current_loop = asyncio.get_running_loop()
            while not self._close_done.is_set():
                if (
                    self._close_async_event is not None and
                    self._close_async_loop is current_loop
                ):
                    try:
                        await asyncio.wait_for(
                            self._close_async_event.wait(),
                            timeout=heartbeat,
                        )
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(heartbeat)
            error = self._close_error
            self._close_done = None
            self._close_async_event = None
            self._close_async_loop = None
            self._close_error = None
            if error is not None:
                raise RuntimeError("Polling watcher failed to shut down") from error
        self._snapshot.clear()

    async def _scan_in_executor(self):
        if self._executor is None:
            raise RuntimeError("Polling watcher is not running")
        scan_future = self.loop.run_in_executor(self._executor, self._scan)
        # Wake immediately when scan_future completes while keeping a bounded
        # heartbeat for embedded/sandboxed loops with delayed thread wakeups.
        heartbeat = min(self.interval, 0.1)
        while not scan_future.done():
            await asyncio.wait({scan_future}, timeout=heartbeat)
        return await scan_future

    async def _poll_loop(self):
        while self._running:
            try:
                # Directory scans are blocking I/O. Keep them off the caller's
                # asyncio loop so a slow network mount cannot stall the app.
                new_snapshot = await self._scan_in_executor()
                if not self._running:
                    break
                self._diff(self._snapshot, new_snapshot)
                self._snapshot = new_snapshot
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.loop.call_exception_handler({
                    "message": "yyds-fswatch polling scan failed",
                    "exception": exc,
                })
                # Avoid a tight retry loop if a scan repeatedly fails.
                await asyncio.sleep(self.interval)

    def _scan(self) -> Dict[Path, Tuple[int, int, bool, int, int]]:
        snapshot = {}
        for p in self.paths:
            if not self._running:
                break
            if not p.exists():
                continue
            resolved = p.resolve()
            self._scan_path(
                p,
                snapshot,
                is_root=True,
                recurse=self._should_recurse_into(resolved, resolved=True),
                canonical=(p == resolved),
            )
        return snapshot

    def _scan_path(self, path: Path, snapshot: dict, is_root: bool = False,
                   recurse: bool = False, entry_stat=None, canonical: bool = False):
        if not self._running:
            return
        try:
            st = entry_stat if entry_stat is not None else path.stat()
            is_dir = stat_module.S_ISDIR(st.st_mode)
            snapshot[path] = (st.st_mtime_ns, st.st_size, is_dir, st.st_dev, st.st_ino)
        except OSError:
            return

        if is_dir and (recurse or is_root):
            filt = self._ignore_filter
            ignore_fn = None
            if filt is not None:
                if canonical:
                    ignore_fn = getattr(filt, "is_ignored_canonical", None) or getattr(filt, "is_ignored", None)
                else:
                    ignore_fn = getattr(filt, "is_ignored", None)
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        if not self._running:
                            return
                        entry_path = Path(entry.path)
                        try:
                            entry_is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue

                        # Apply ignore filter
                        if ignore_fn is not None and ignore_fn(entry_path, entry_is_dir):
                            continue

                        try:
                            entry_st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue

                        if entry_is_dir and recurse:
                            self._scan_path(
                                entry_path,
                                snapshot,
                                is_root=False,
                                recurse=True,
                                entry_stat=entry_st,
                                canonical=canonical,
                            )
                        else:
                            snapshot[entry_path] = (
                                entry_st.st_mtime_ns,
                                entry_st.st_size,
                                entry_is_dir,
                                entry_st.st_dev,
                                entry_st.st_ino,
                            )
            except OSError:
                pass

    @staticmethod
    def _identity(state):
        # Some network/virtual file systems report inode 0 for every entry;
        # those values cannot safely be used to infer a rename.
        if not state[4]:
            return None
        if state[2]:
            return state[3], state[4], True
        # Local filesystems such as ext4 immediately reuse freed inode numbers
        # when one file is unlinked and an unrelated file is created in the
        # same poll window. Require size and mtime to match for file moves, as
        # rename() preserves both while unlink+create gets a fresh mtime/size.
        return state[3], state[4], False, state[0], state[1]

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
        emitted_dir_map: Dict[Path, Path] = {}
        for old_path, new_path, is_dir in sorted(
            move_pairs,
            key=lambda item: (len(item[0].parts), str(item[0]), str(item[1])),
        ):
            covered = False
            for old_parent in old_path.parents:
                new_parent = emitted_dir_map.get(old_parent)
                if new_parent is None:
                    continue
                if new_path == new_parent / old_path.relative_to(old_parent):
                    covered = True
                    break
            if covered:
                continue
            self.on_raw_event("moved", old_path, is_dir, dest_path=new_path)
            if is_dir:
                emitted_dir_map[old_path] = new_path

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
            old_state = old[path]
            new_state = new[path]
            if old_state[2] != new_state[2]:
                self.on_raw_event("deleted", path, old_state[2])
                self.on_raw_event("created", path, new_state[2])
            elif new_state[2]:
                # Directory mtime/size changes whenever children are added or
                # removed; only emit modified if its inode identity changed.
                if old_state[4] and new_state[4] and (old_state[3], old_state[4]) != (new_state[3], new_state[4]):
                    self.on_raw_event("modified", path, True)
            elif old_state != new_state:
                self.on_raw_event("modified", path, False)
