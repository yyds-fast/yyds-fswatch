# -*- coding: utf-8 -*-

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from yyds_fswatch import FSWatcher
from yyds_fswatch._platforms._polling import PollingWatcher

if sys.platform.startswith("linux"):
    from yyds_fswatch._platforms._linux import InotifyMask
else:
    InotifyMask = None


class TestFSWatcherRegressions(unittest.TestCase):
    def setUp(self):
        self.base_dir = Path(tempfile.mkdtemp(dir=Path(".").resolve())).resolve()
        self.root = self.base_dir / "root"
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def test_public_stop_terminates_blocking_start(self):
        watcher = FSWatcher(self.root)
        thread = threading.Thread(target=watcher.start)
        thread.start()

        self.assertTrue(watcher.wait_until_ready(timeout=2.0))
        watcher.stop()
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertFalse(watcher.is_ready)

    def test_missing_file_can_be_declared_explicitly(self):
        target = self.root / "future.txt"
        watcher = FSWatcher(target, target_type="file")

        self.assertIn(target, watcher._file_targets)
        self.assertEqual(watcher.paths, [self.root])

        async def run():
            async with watcher as active:
                target.write_text("created", encoding="utf-8")
                return await asyncio.wait_for(active.__anext__(), timeout=1.0)

        event = asyncio.run(run())
        self.assertEqual(event.event_type, "created")
        self.assertEqual(Path(event.src_path), target)

    def test_atomic_replace_reports_target_not_temporary_file(self):
        target = self.root / "target.txt"
        target.write_text("old", encoding="utf-8")

        async def run():
            async with FSWatcher(
                target,
                debounce=0.01,
                use_gitignore=False,
                ignore_patterns=["*.tmp"],
                ignore_common_dirs=False,
            ) as watcher:
                temporary = self.root / ".target.tmp"
                temporary.write_text("new", encoding="utf-8")
                os.replace(temporary, target)
                return await asyncio.wait_for(watcher.__anext__(), timeout=1.0)

        event = asyncio.run(run())
        self.assertEqual(event.event_type, "modified")
        self.assertEqual(Path(event.src_path), target)
        self.assertEqual(event.dest_path, "")

    def test_existing_file_created_event_is_normalized_to_modified(self):
        target = self.root / "target.txt"
        target.write_text("old", encoding="utf-8")

        async def run():
            async with FSWatcher(
                target,
                debounce=0,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                watcher._on_raw_event("created", target, False)
                return await asyncio.wait_for(watcher.__anext__(), timeout=1.0)

        event = asyncio.run(run())
        self.assertEqual(event.event_type, "modified")
        self.assertEqual(Path(event.src_path), target)

    def test_mixed_directory_and_file_targets_keep_directory_events(self):
        selected = self.root / "selected.txt"
        selected.write_text("old", encoding="utf-8")
        other = self.root / "other.txt"

        async def run():
            expected_paths = {selected, other}
            events = {}
            async with FSWatcher(
                [self.root, selected],
                debounce=0.01,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                other.write_text("other", encoding="utf-8")
                selected.write_text("new", encoding="utf-8")
                deadline = asyncio.get_running_loop().time() + 2.0
                while expected_paths - set(events):
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        event = await asyncio.wait_for(watcher.__anext__(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    event_path = Path(event.src_path).resolve()
                    if event_path in expected_paths:
                        events[event_path] = event
            return events

        events = asyncio.run(run())
        self.assertEqual(set(events), {selected, other}, f"Expected file events, got: {events}")
        self.assertEqual(events[other].event_type, "created")
        self.assertEqual(events[selected].event_type, "modified")

    def test_bounded_queue_emits_overflow(self):
        async def run():
            async with FSWatcher(
                self.root,
                debounce=0,
                max_queue_size=1,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                watcher._push_to_debounce("created", self.root / "one", False)
                watcher._push_to_debounce("created", self.root / "two", False)
                event = await asyncio.wait_for(watcher.__anext__(), timeout=1.0)
                return event, watcher.dropped_event_count

        event, dropped = asyncio.run(run())
        self.assertEqual(event.event_type, "overflow")
        self.assertEqual(dropped, 1)

    def test_event_loop_raw_events_skip_threadsafe_schedule(self):
        async def run():
            async with FSWatcher(
                self.root,
                debounce=0,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                loop = asyncio.get_running_loop()
                original_schedule = loop.call_soon_threadsafe
                calls = []

                def record_threadsafe_schedule(*args, **kwargs):
                    calls.append((args, kwargs))

                loop.call_soon_threadsafe = record_threadsafe_schedule
                try:
                    target = self.root / "direct-loop-event.txt"
                    watcher._on_raw_event("created", target, False)
                    self.assertEqual(calls, [])
                    return await asyncio.wait_for(watcher.__anext__(), timeout=1.0)
                finally:
                    loop.call_soon_threadsafe = original_schedule

        event = asyncio.run(run())
        self.assertEqual(event.event_type, "created")
        self.assertEqual(Path(event.src_path).name, "direct-loop-event.txt")

    def test_stop_sentinel_survives_a_full_queue(self):
        async def run():
            async with FSWatcher(
                self.root,
                debounce=0,
                max_queue_size=1,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                watcher._push_to_debounce("created", self.root / "queued", False)
                watcher.stop()
                watcher._push_to_debounce("created", self.root / "late", False)
                await asyncio.sleep(0)
                with self.assertRaises(StopAsyncIteration):
                    await watcher.__anext__()

        asyncio.run(run())

    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify-specific regression")
    def test_moved_out_directory_does_not_leak_stale_events(self):
        watched = self.root / "sub"
        watched.mkdir()
        outside = self.base_dir / "outside"
        outside.mkdir()

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0.01,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                moved = outside / "sub"
                watched.rename(moved)
                deleted = await asyncio.wait_for(watcher.__anext__(), timeout=1.0)
                await asyncio.sleep(0.08)
                (moved / "outside.txt").write_text("x", encoding="utf-8")
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(watcher.__anext__(), timeout=0.15)
                stale = watched in watcher._watcher._path_to_wd
                return deleted, stale

        deleted, stale = asyncio.run(run())
        self.assertEqual(deleted.event_type, "deleted")
        self.assertEqual(Path(deleted.src_path), watched)
        self.assertFalse(stale)

    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify-specific regression")
    def test_linux_overflow_rebuilds_watch_tree(self):
        async def run():
            async with FSWatcher(
                self.root,
                debounce=0,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                backend = watcher._watcher
                backend._dispatch_event(-1, InotifyMask.IN_Q_OVERFLOW, 0, "")
                event = await asyncio.wait_for(watcher.__anext__(), timeout=1.0)
                await asyncio.sleep(0.02)
                return event, backend.is_running, self.root in backend._path_to_wd

        event, running, root_registered = asyncio.run(run())
        self.assertEqual(event.event_type, "overflow")
        self.assertTrue(running)
        self.assertTrue(root_registered)

    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify-specific regression")
    def test_linux_recovers_after_root_is_recreated_between_probes(self):
        """A valid inotify fd must not hide a dropped root watch."""
        target = self.root / "after-recreate.txt"

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0,
                recovery_interval=0.02,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                shutil.rmtree(self.root)
                self.root.mkdir()

                # The root exists again before the recovery loop's first
                # probe, so path-existence checks alone cannot detect loss of
                # the original inotify watch.
                await asyncio.sleep(0.08)
                target.write_text("recreated", encoding="utf-8")

                deadline = asyncio.get_running_loop().time() + 1.0
                while asyncio.get_running_loop().time() < deadline:
                    event = await asyncio.wait_for(watcher.__anext__(), timeout=0.2)
                    if Path(event.src_path) == target:
                        return event
                self.fail("recreated root did not resume file events")

        event = asyncio.run(run())
        self.assertEqual(event.event_type, "created")

    def test_polling_close_keeps_event_loop_responsive(self):
        async def run():
            watcher = FSWatcher(
                self.root,
                backend="polling",
                poll_interval=0.01,
                use_gitignore=False,
                ignore_common_dirs=False,
            )
            await watcher.__aenter__()
            entered = True
            release_scan = threading.Event()
            scan_started = threading.Event()
            release_timer = None

            try:
                backend = watcher._watcher
                original_scan = backend._scan

                def slow_scan():
                    scan_started.set()
                    release_scan.wait(timeout=1.0)
                    return original_scan()

                backend._scan = slow_scan
                for _ in range(30):
                    if scan_started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(scan_started.is_set())

                tick_before_release = []
                loop = asyncio.get_running_loop()
                loop.call_later(
                    0.02,
                    lambda: tick_before_release.append(not release_scan.is_set()),
                )
                release_timer = threading.Timer(0.15, release_scan.set)
                release_timer.daemon = True
                release_timer.start()

                await watcher.__aexit__(None, None, None)
                entered = False
                return tick_before_release
            finally:
                release_scan.set()
                if release_timer is not None:
                    release_timer.cancel()
                if entered:
                    await watcher.__aexit__(None, None, None)

        ticks = asyncio.run(run())
        self.assertEqual(ticks, [True])

    def test_polling_initial_scan_keeps_event_loop_responsive(self):
        async def run():
            watcher = FSWatcher(
                self.root,
                backend="polling",
                poll_interval=0.01,
                use_gitignore=False,
                ignore_common_dirs=False,
            )
            release_scan = threading.Event()
            scan_started = threading.Event()
            release_timer = None
            entered = False
            original_scan = PollingWatcher._scan

            def slow_scan(backend):
                scan_started.set()
                release_scan.wait(timeout=1.0)
                return original_scan(backend)

            PollingWatcher._scan = slow_scan
            try:
                tick_before_release = []
                loop = asyncio.get_running_loop()
                loop.call_later(
                    0.02,
                    lambda: tick_before_release.append(not release_scan.is_set()),
                )
                release_timer = threading.Timer(0.15, release_scan.set)
                release_timer.daemon = True
                release_timer.start()

                await watcher.__aenter__()
                entered = True
                self.assertTrue(scan_started.is_set())
                return tick_before_release
            finally:
                PollingWatcher._scan = original_scan
                release_scan.set()
                if release_timer is not None:
                    release_timer.cancel()
                if entered:
                    await watcher.__aexit__(None, None, None)

        ticks = asyncio.run(run())
        self.assertEqual(ticks, [True])

    def test_polling_diff_detects_directory_move_once(self):
        old_dir = self.root / "old"
        new_dir = self.root / "new"
        events = []
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        watcher = PollingWatcher(
            [self.root], True, loop,
            lambda *args, **kwargs: events.append((args, kwargs)),
        )
        old = {
            old_dir: (1, 0, True, 1, 10),
            old_dir / "child": (1, 1, False, 1, 11),
        }
        new = {
            new_dir: (1, 0, True, 1, 10),
            new_dir / "child": (1, 1, False, 1, 11),
        }

        watcher._diff(old, new)

        self.assertEqual(len(events), 1)
        args, kwargs = events[0]
        self.assertEqual(args[:3], ("moved", old_dir, True))
        self.assertEqual(kwargs["dest_path"], new_dir)

    def test_polling_diff_orders_tree_events(self):
        parent = self.root / "parent"
        child = parent / "child.txt"
        root_state = (1, 0, True, 1, 1)
        parent_state = (1, 0, True, 1, 2)
        child_state = (1, 1, False, 1, 3)
        events = []
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        watcher = PollingWatcher(
            [self.root], True, loop,
            lambda event_type, path, is_dir, **kwargs: events.append((event_type, path)),
        )

        watcher._diff(
            {self.root: root_state, parent: parent_state, child: child_state},
            {self.root: root_state},
        )
        self.assertEqual(
            events,
            [("deleted", child), ("deleted", parent)],
        )

        events.clear()
        watcher._diff(
            {self.root: root_state},
            {self.root: root_state, parent: parent_state, child: child_state},
        )
        self.assertEqual(
            events,
            [("created", parent), ("created", child)],
        )

    def test_forced_polling_backend_detects_move(self):
        old_path = self.root / "old.txt"
        new_path = self.root / "new.txt"
        old_path.write_text("content", encoding="utf-8")

        async def run():
            async with FSWatcher(
                self.root,
                backend="polling",
                poll_interval=0.02,
                debounce=0,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                old_path.rename(new_path)
                deadline = asyncio.get_running_loop().time() + 1.0
                while asyncio.get_running_loop().time() < deadline:
                    event = await asyncio.wait_for(watcher.__anext__(), timeout=0.5)
                    if event.event_type == "moved":
                        return event, watcher.backend
                self.fail("polling backend did not emit a moved event")

        event, backend = asyncio.run(run())
        self.assertEqual(backend, "PollingWatcher")
        self.assertEqual(Path(event.src_path), old_path)
        self.assertEqual(Path(event.dest_path), new_path)


if __name__ == "__main__":
    unittest.main()
