# -*- coding: utf-8 -*-

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
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

    @unittest.skipUnless(sys.platform == "win32", "Windows-specific regression")
    def test_windows_native_stop_cancels_blocking_read(self):
        async def run():
            watcher = FSWatcher(
                self.root,
                backend="native",
                use_gitignore=False,
                ignore_common_dirs=False,
            )
            entered = False
            try:
                await watcher.__aenter__()
                entered = True
                backend = watcher._watcher
                deadline = asyncio.get_running_loop().time() + 1.0
                while not backend._worker_thread_ids:
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(backend._worker_thread_ids)

                # Give the worker a chance to enter ReadDirectoryChangesW.
                await asyncio.sleep(0.05)
                started = time.monotonic()
                await watcher.__aexit__(None, None, None)
                entered = False
                return time.monotonic() - started
            finally:
                if entered:
                    await watcher.__aexit__(None, None, None)

        elapsed = asyncio.run(run())
        self.assertLess(elapsed, 0.75)

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

    def test_directory_mode_observed_file_created_event_is_normalized_to_modified(self):
        target = self.root / "target.txt"

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                watcher._on_raw_event("created", target, False)
                first = await asyncio.wait_for(watcher.__anext__(), timeout=1.0)
                # Atomic replace on FSEvents/ReadDirectoryChangesW can surface
                # as a plain creation event on an already-observed file.
                watcher._on_raw_event("created", target, False)
                second = await asyncio.wait_for(watcher.__anext__(), timeout=1.0)
                return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first.event_type, "created")
        self.assertEqual(second.event_type, "modified")
        self.assertEqual(Path(second.src_path), target)

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

    def test_forced_polling_backend_reports_rename(self):
        old_path = self.root / "old.txt"
        new_path = self.root / "new.txt"
        old_path.write_text("content", encoding="utf-8")

        async def run():
            events = []
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
                    remaining = deadline - asyncio.get_running_loop().time()
                    try:
                        event = await asyncio.wait_for(
                            watcher.__anext__(), timeout=min(remaining, 0.2)
                        )
                    except asyncio.TimeoutError:
                        continue
                    events.append(event)
                    moved = any(
                        event.event_type == "moved" and
                        Path(event.src_path) == old_path and
                        Path(event.dest_path) == new_path
                        for event in events
                    )
                    deleted_old = any(
                        event.event_type == "deleted" and Path(event.src_path) == old_path
                        for event in events
                    )
                    created_new = any(
                        event.event_type == "created" and Path(event.src_path) == new_path
                        for event in events
                    )
                    if moved or (deleted_old and created_new):
                        return events, watcher.backend
                self.fail("polling backend did not report the renamed file")

        events, backend = asyncio.run(run())
        self.assertEqual(backend, "PollingWatcher")
        moved_events = [event for event in events if event.event_type == "moved"]
        if moved_events:
            self.assertEqual(Path(moved_events[0].src_path), old_path)
            self.assertEqual(Path(moved_events[0].dest_path), new_path)
        else:
            event_types_and_paths = [
                (event.event_type, Path(event.src_path)) for event in events
            ]
            self.assertIn(("deleted", old_path), event_types_and_paths)
            self.assertIn(("created", new_path), event_types_and_paths)

    def test_single_file_watch_does_not_recurse_parent_subdirectories(self):
        target = self.root / "target.txt"
        target.write_text("x", encoding="utf-8")
        subdir = self.root / "nested"
        subdir.mkdir()
        (subdir / "deep.txt").write_text("y", encoding="utf-8")

        async def run():
            async with FSWatcher(
                target,
                recursive=True,
                backend="polling",
                poll_interval=0.05,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as poll_watcher:
                self.assertFalse(poll_watcher._watcher.recursive)
                self.assertNotIn(subdir / "deep.txt", poll_watcher._watcher._snapshot)

            if sys.platform.startswith("linux"):
                async with FSWatcher(
                    target,
                    recursive=True,
                    backend="native",
                    use_gitignore=False,
                    ignore_common_dirs=False,
                ) as native_watcher:
                    self.assertFalse(native_watcher._watcher.recursive)
                    self.assertIn(self.root, native_watcher._watcher._path_to_wd)
                    self.assertNotIn(subdir, native_watcher._watcher._path_to_wd)

            # Mixed directory + file targets must only recurse the directory target.
            other_dir = self.base_dir / "other_dir"
            other_dir.mkdir()
            other_sub = other_dir / "sub"
            other_sub.mkdir()
            other_file = other_dir / "picked.txt"
            other_file.write_text("z", encoding="utf-8")

            async with FSWatcher(
                [self.root, other_file],
                recursive=True,
                backend="polling",
                poll_interval=0.05,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as mixed_watcher:
                self.assertIn(subdir / "deep.txt", mixed_watcher._watcher._snapshot)
                self.assertIn(other_file, mixed_watcher._watcher._snapshot)
                self.assertIn(other_sub, mixed_watcher._watcher._snapshot)
                self.assertNotIn(other_sub / "never", mixed_watcher._watcher._snapshot)

        asyncio.run(run())

    def test_dot_star_ignore_pattern_keeps_root_watched(self):
        async def run():
            async with FSWatcher(
                self.root,
                debounce=0,
                recovery_interval=0.05,
                ignore_patterns=[".*"],
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                if sys.platform.startswith("linux"):
                    self.assertIn(self.root, watcher._watcher._path_to_wd)
                (self.root / ".hidden").write_text("secret", encoding="utf-8")
                visible = self.root / "visible.txt"
                visible.write_text("ok", encoding="utf-8")
                deadline = asyncio.get_running_loop().time() + 1.0
                while asyncio.get_running_loop().time() < deadline:
                    event = await asyncio.wait_for(watcher.__anext__(), timeout=0.3)
                    if Path(event.src_path).name == "visible.txt":
                        return event, watcher.is_running
                self.fail("did not receive event for visible.txt with ignore_patterns=['.*']")

        event, is_running = asyncio.run(run())
        self.assertEqual(event.event_type, "created")
        self.assertTrue(is_running)

    def test_flush_event_cancels_pending_debounce_timer(self):
        target = self.root / "debounced.txt"

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0.12,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                watcher._push_to_debounce("modified", target, False)
                # Force early flush before debounce expires.
                watcher._push_to_debounce("overflow", self.root, False)
                ev1 = await asyncio.wait_for(watcher.__anext__(), timeout=0.1)
                ev2 = await asyncio.wait_for(watcher.__anext__(), timeout=0.1)
                self.assertEqual((ev1.event_type, ev2.event_type), ("modified", "overflow"))

                # Push a new event right away. If the old timer wasn't cancelled,
                # it would fire prematurely before the new 0.12s window completes.
                watcher._push_to_debounce("modified", target, False)
                await asyncio.sleep(0.06)
                self.assertTrue(watcher._queue.empty())

                ev3 = await asyncio.wait_for(watcher.__anext__(), timeout=0.3)
                self.assertEqual(ev3.event_type, "modified")
                self.assertEqual(Path(ev3.src_path), target)

        asyncio.run(run())

    def test_directory_mode_atomic_replace_after_observed_reports_modified(self):
        target = self.root / "target.txt"

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0.05,
                use_gitignore=False,
                ignore_patterns=["*.tmp"],
                ignore_common_dirs=False,
            ) as watcher:
                target.write_text("v1", encoding="utf-8")
                deadline = asyncio.get_running_loop().time() + 2.0
                while asyncio.get_running_loop().time() < deadline:
                    remaining = max(deadline - asyncio.get_running_loop().time(), 0.05)
                    event = await asyncio.wait_for(watcher.__anext__(), timeout=remaining)
                    if Path(event.src_path) == target:
                        self.assertEqual(event.event_type, "created")
                        break
                else:
                    self.fail("did not receive initial created event for target.txt")

                # Let any trailing write metadata / debounce flush settle before replacing.
                await asyncio.sleep(0.08)
                while not watcher._queue.empty():
                    await watcher.__anext__()

                temp = self.root / ".target.tmp"
                temp.write_text("v2", encoding="utf-8")
                os.replace(temp, target)

                deadline = asyncio.get_running_loop().time() + 2.0
                while asyncio.get_running_loop().time() < deadline:
                    remaining = max(deadline - asyncio.get_running_loop().time(), 0.05)
                    event = await asyncio.wait_for(watcher.__anext__(), timeout=remaining)
                    if Path(event.src_path) == target:
                        return event
                self.fail("did not receive replacement event for target.txt")

        event = asyncio.run(run())
        self.assertEqual(event.event_type, "modified")
        self.assertEqual(Path(event.src_path), target)

    def test_symlink_inside_root_reports_link_creation_and_deletion(self):
        outside = self.base_dir / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.root / "link.txt"

        async def next_for_link(watcher):
            deadline = asyncio.get_running_loop().time() + 1.0
            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                event = await asyncio.wait_for(
                    watcher.__anext__(), timeout=max(remaining, 0.05)
                )
                if Path(event.src_path) == link:
                    return event
            self.fail("did not receive expected symlink event")

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0.01,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                try:
                    os.symlink(outside, link)
                except OSError as exc:
                    self.skipTest(f"symlinks unavailable: {exc}")

                created = await next_for_link(watcher)
                link.unlink()
                deleted = await next_for_link(watcher)
                return created, deleted

        created, deleted = asyncio.run(run())
        self.assertEqual(created.event_type, "created")
        self.assertEqual(Path(created.src_path), link)
        self.assertEqual(deleted.event_type, "deleted")
        self.assertEqual(Path(deleted.src_path), link)

    def test_backpressure_overflow_rearms_after_subsequent_event_queued(self):
        async def run():
            async with FSWatcher(
                self.root,
                debounce=0,
                max_queue_size=2,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                watcher._push_to_debounce("created", self.root / "one", False)
                watcher._push_to_debounce("created", self.root / "two", False)
                # Triggers first overflow: queue becomes [two, overflow_1].
                watcher._push_to_debounce("created", self.root / "three", False)

                first = await asyncio.wait_for(watcher.__anext__(), timeout=0.5)
                self.assertEqual(Path(first.src_path).name, "two")

                # Queue now has 1 free slot; queueing "four" should re-arm overflow.
                watcher._push_to_debounce("created", self.root / "four", False)
                # Queue is full again [overflow_1, four]; "five" triggers overflow_2.
                watcher._push_to_debounce("created", self.root / "five", False)

                ev_a = await asyncio.wait_for(watcher.__anext__(), timeout=0.5)
                ev_b = await asyncio.wait_for(watcher.__anext__(), timeout=0.5)
                return ev_a, ev_b

        ev_a, ev_b = asyncio.run(run())
        self.assertEqual(Path(ev_a.src_path).name, "four")
        self.assertEqual(ev_b.event_type, "overflow")

    def test_polling_diff_ignores_directory_mtime_changes_on_child_edits(self):
        sub = self.root / "sub"
        child = sub / "child.txt"
        events = []
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        watcher = PollingWatcher(
            [self.root], True, loop,
            lambda event_type, path, is_dir, **kwargs: events.append((event_type, path, is_dir)),
        )
        old = {
            self.root: (100, 4096, True, 1, 1),
            sub: (100, 4096, True, 1, 2),
        }
        new = {
            self.root: (200, 4096, True, 1, 1),
            sub: (200, 8192, True, 1, 2),
            child: (200, 10, False, 1, 3),
        }
        watcher._diff(old, new)
        self.assertEqual(events, [("created", child, False)])

    def test_polling_startup_and_close_avoid_artificial_heartbeat_delay(self):
        async def run():
            started = time.monotonic()
            async with FSWatcher(
                self.root,
                backend="polling",
                poll_interval=0.5,
                use_gitignore=False,
                ignore_common_dirs=False,
            ):
                pass
            return time.monotonic() - started

        elapsed = asyncio.run(run())
        self.assertLess(elapsed, 0.15)

    def test_stop_immediately_after_thread_start_does_not_hang(self):
        watcher = FSWatcher(self.root, use_gitignore=False, ignore_common_dirs=False)
        thread = threading.Thread(target=watcher.start)
        thread.start()
        watcher.stop()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify-specific regression")
    def test_linux_root_move_self_and_recreate_recovers(self):
        moved_root = self.base_dir / "moved_root"
        stale_file = moved_root / "stale.txt"
        new_file = self.root / "recovered.txt"

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0.01,
                recovery_interval=0.03,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                os.rename(self.root, moved_root)
                self.root.mkdir()
                await asyncio.sleep(0.1)
                stale_file.write_text("stale", encoding="utf-8")
                new_file.write_text("fresh", encoding="utf-8")

                events = []
                deadline = asyncio.get_running_loop().time() + 1.0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        event = await asyncio.wait_for(watcher.__anext__(), timeout=0.15)
                    except asyncio.TimeoutError:
                        continue
                    events.append(event)
                    if Path(event.src_path) == new_file and event.event_type == "created":
                        return events
                self.fail(f"recreated root after move did not resume; got: {events}")

        events = asyncio.run(run())
        self.assertFalse(any("stale.txt" in e.src_path for e in events))

    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify-specific regression")
    def test_linux_rapid_nested_dir_creation_captures_children(self):
        nested_dir = self.root / "a" / "b" / "c"
        nested_file = nested_dir / "deep.txt"

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0.01,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                os.makedirs(nested_dir)
                nested_file.write_text("hello", encoding="utf-8")

                deadline = asyncio.get_running_loop().time() + 1.0
                while asyncio.get_running_loop().time() < deadline:
                    event = await asyncio.wait_for(watcher.__anext__(), timeout=0.3)
                    if Path(event.src_path) == nested_file:
                        return event
                self.fail("nested file created inside rapid makedirs was missed")

        event = asyncio.run(run())
        self.assertEqual(event.event_type, "created")

    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify-specific regression")
    def test_linux_ignored_dir_renamed_to_visible_is_watched(self):
        hidden_dir = self.root / ".staging"
        hidden_dir.mkdir()
        (hidden_dir / "before.txt").write_text("1", encoding="utf-8")
        visible_dir = self.root / "visible"

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0.01,
                ignore_patterns=[".*"],
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                os.rename(hidden_dir, visible_dir)
                await asyncio.sleep(0.05)
                (visible_dir / "after.txt").write_text("2", encoding="utf-8")

                seen = set()
                deadline = asyncio.get_running_loop().time() + 1.0
                while asyncio.get_running_loop().time() < deadline:
                    event = await asyncio.wait_for(watcher.__anext__(), timeout=0.3)
                    seen.add((event.event_type, Path(event.src_path)))
                    if ("created", visible_dir / "after.txt") in seen:
                        return seen
                self.fail(f"renamed visible directory was not watched; seen: {seen}")

        seen = asyncio.run(run())
        self.assertIn(("created", visible_dir / "after.txt"), seen)
        self.assertIn(("created", visible_dir / "before.txt"), seen)

    @unittest.skipUnless(sys.platform == "darwin", "macOS-specific regression")
    def test_darwin_compound_flags_and_typed_renames(self):
        import ctypes
        from yyds_fswatch._platforms._darwin import (
            DarwinWatcher,
            kFSEventStreamEventFlagItemCreated,
            kFSEventStreamEventFlagItemIsDir,
            kFSEventStreamEventFlagItemRemoved,
            kFSEventStreamEventFlagItemRenamed,
        )

        events = []
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        watcher = DarwinWatcher(
            [self.root], True, loop,
            lambda *args, **kwargs: events.append((args, kwargs)),
        )
        gone_file = self.root / "gone.txt"
        old_file = self.root / "old.txt"
        new_file = self.root / "new.txt"
        old_dir = self.root / "old_dir"
        new_dir = self.root / "new_dir"
        new_file.write_text("x", encoding="utf-8")
        new_dir.mkdir()

        raw_paths = [
            os.fsencode(gone_file),
            os.fsencode(old_file),
            os.fsencode(new_dir),
            os.fsencode(old_dir),
            os.fsencode(new_file),
        ]
        paths_arr = (ctypes.c_char_p * len(raw_paths))(*raw_paths)
        flags_arr = (ctypes.c_uint32 * len(raw_paths))(
            kFSEventStreamEventFlagItemCreated | kFSEventStreamEventFlagItemRemoved,
            kFSEventStreamEventFlagItemRenamed,
            kFSEventStreamEventFlagItemRenamed | kFSEventStreamEventFlagItemIsDir,
            kFSEventStreamEventFlagItemRenamed | kFSEventStreamEventFlagItemIsDir,
            kFSEventStreamEventFlagItemRenamed,
        )
        ids_arr = (ctypes.c_uint64 * len(raw_paths))(1, 2, 3, 4, 5)
        watcher._py_callback(
            None,
            None,
            len(raw_paths),
            ctypes.cast(paths_arr, ctypes.c_void_p),
            flags_arr,
            ids_arr,
        )

        self.assertIn((("created", gone_file, False), {}), events)
        self.assertIn((("deleted", gone_file, False), {}), events)
        self.assertIn((("moved", old_file, False), {"dest_path": new_file}), events)
        self.assertIn((("moved", old_dir, True), {"dest_path": new_dir}), events)

    def test_polling_diff_does_not_misreport_reused_inode_unlink_plus_create_as_move(self):
        unlinked = self.root / "a.txt"
        created_unrelated = self.root / "b.json"
        renamed_old = self.root / "c.txt"
        renamed_new = self.root / "d.txt"
        events = []
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        watcher = PollingWatcher(
            [self.root], True, loop,
            lambda event_type, path, is_dir, **kwargs: events.append(
                (event_type, path, is_dir, kwargs.get("dest_path"))
            ),
        )
        old = {
            self.root: (100, 4096, True, 1, 1),
            unlinked: (100, 10, False, 1, 42),
            renamed_old: (300, 15, False, 1, 99),
        }
        new = {
            self.root: (200, 4096, True, 1, 1),
            # Same inode 42 reused immediately, but fresh mtime & size:
            created_unrelated: (200, 25, False, 1, 42),
            # Genuine rename preserves inode, mtime, and size:
            renamed_new: (300, 15, False, 1, 99),
        }
        watcher._diff(old, new)
        self.assertIn(("moved", renamed_old, False, renamed_new), events)
        self.assertIn(("deleted", unlinked, False, None), events)
        self.assertIn(("created", created_unrelated, False, None), events)

    def test_debounce_file_to_directory_replacement_emits_delete_then_create(self):
        target = self.root / "morph"

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0.05,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                watcher._push_to_debounce("deleted", target, False)
                watcher._push_to_debounce("created", target, True)
                matched = []
                deadline = asyncio.get_running_loop().time() + 1.5
                while len(matched) < 2 and asyncio.get_running_loop().time() < deadline:
                    remaining = max(deadline - asyncio.get_running_loop().time(), 0.05)
                    ev = await asyncio.wait_for(watcher.__anext__(), timeout=remaining)
                    if Path(ev.src_path) == target:
                        matched.append(ev)
                self.assertEqual(len(matched), 2)
                return matched[0], matched[1]

        ev1, ev2 = asyncio.run(run())
        self.assertEqual((ev1.event_type, ev1.is_directory), ("deleted", False))
        self.assertEqual((ev2.event_type, ev2.is_directory), ("created", True))

    def test_directory_move_flushes_pending_child_events_first(self):
        dir_a = self.root / "dir_a"
        dir_b = self.root / "dir_b"
        child = dir_a / "child.txt"

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0.25,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                watcher._push_to_debounce("created", child, False)
                watcher._push_to_debounce("moved", dir_a, True, dest_path=dir_b)
                matched = []
                deadline = asyncio.get_running_loop().time() + 1.5
                while len(matched) < 2 and asyncio.get_running_loop().time() < deadline:
                    remaining = max(deadline - asyncio.get_running_loop().time(), 0.05)
                    ev = await asyncio.wait_for(watcher.__anext__(), timeout=remaining)
                    if Path(ev.src_path) in {child, dir_a}:
                        matched.append(ev)
                self.assertEqual(len(matched), 2)
                return matched[0], matched[1]

        ev1, ev2 = asyncio.run(run())
        self.assertEqual((ev1.event_type, Path(ev1.src_path)), ("created", child))
        self.assertEqual(
            (ev2.event_type, Path(ev2.src_path), Path(ev2.dest_path)),
            ("moved", dir_a, dir_b),
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify-specific regression")
    def test_linux_dynamic_gitignore_unignore_reconciles_directory_watches(self):
        gitignore = self.root / ".gitignore"
        logs_dir = self.root / "logs"
        gitignore.write_text("logs/\n", encoding="utf-8")
        logs_dir.mkdir()

        async def run():
            async with FSWatcher(
                self.root,
                debounce=0.01,
                use_gitignore=True,
                ignore_common_dirs=False,
            ) as watcher:
                self.assertNotIn(logs_dir, watcher._watcher._path_to_wd)

                gitignore.write_text("", encoding="utf-8")
                deadline = asyncio.get_running_loop().time() + 1.0
                while logs_dir not in watcher._watcher._path_to_wd:
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail("logs/ was not registered after .gitignore un-ignored it")
                    await asyncio.sleep(0.01)

                log_file = logs_dir / "app.log"
                log_file.write_text("hello", encoding="utf-8")
                deadline = asyncio.get_running_loop().time() + 1.0
                while asyncio.get_running_loop().time() < deadline:
                    event = await asyncio.wait_for(watcher.__anext__(), timeout=0.3)
                    if Path(event.src_path) == log_file:
                        return event
                self.fail("did not receive event inside newly un-ignored logs/")

        event = asyncio.run(run())
        self.assertEqual(event.event_type, "created")

    def test_recovery_emits_initial_entries_created_with_missing_root(self):
        missing_root = self.base_dir / "future_root"
        boot_file = missing_root / "sub" / "boot.txt"

        async def run():
            async with FSWatcher(
                missing_root,
                target_type="directory",
                debounce=0.01,
                recovery_interval=0.03,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                os.makedirs(boot_file.parent)
                boot_file.write_text("ready", encoding="utf-8")

                deadline = asyncio.get_running_loop().time() + 1.5
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        event = await asyncio.wait_for(watcher.__anext__(), timeout=0.3)
                    except asyncio.TimeoutError:
                        continue
                    if Path(event.src_path) == boot_file:
                        return event
                self.fail("did not receive created event for file created together with missing root")

        event = asyncio.run(run())
        self.assertEqual(event.event_type, "created")

    def test_stop_halts_decorator_loop_immediately_despite_queue_backlog(self):
        watcher = FSWatcher(
            self.root,
            debounce=0,
            use_gitignore=False,
            ignore_common_dirs=False,
        )
        handled = []

        @watcher.on("*")
        def on_any(event):
            # Ignore incidental startup events on self.root (e.g., macOS FSEvents)
            if not Path(event.src_path).name.startswith("f_"):
                return
            handled.append(event)
            watcher.stop()

        async def run():
            loop_task = asyncio.create_task(watcher._run_decorator_loop())
            deadline = asyncio.get_running_loop().time() + 3.0
            while not watcher.is_ready and not loop_task.done():
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("watcher did not become ready")
                await asyncio.sleep(0.01)
            if loop_task.done():
                await loop_task
                self.fail("decorator loop exited before backlog was queued")

            for idx in range(50):
                watcher._push_to_debounce("created", self.root / f"f_{idx}.txt", False)

            await asyncio.wait_for(loop_task, timeout=2.0)

        asyncio.run(run())
        self.assertEqual(len(handled), 1)

    def test_concurrent_context_reentry_raises_without_stopping_active_watcher(self):
        async def run():
            async with FSWatcher(
                self.root,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                with self.assertRaises(RuntimeError):
                    watcher.__enter__()
                self.assertTrue(watcher.is_running)

        asyncio.run(run())

    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify-specific regression")
    def test_linux_inotify_fd_has_cloexec_flag(self):
        import fcntl

        async def run():
            async with FSWatcher(
                self.root,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                flags = fcntl.fcntl(watcher._watcher._fd, fcntl.F_GETFD)
                return bool(flags & fcntl.FD_CLOEXEC)

        self.assertTrue(asyncio.run(run()))


if __name__ == "__main__":
    unittest.main()
