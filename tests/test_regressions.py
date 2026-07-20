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

    def test_mixed_directory_and_file_targets_keep_directory_events(self):
        selected = self.root / "selected.txt"
        selected.write_text("old", encoding="utf-8")
        other = self.root / "other.txt"

        async def run():
            events = []
            async with FSWatcher(
                [self.root, selected],
                debounce=0.01,
                use_gitignore=False,
                ignore_common_dirs=False,
            ) as watcher:
                other.write_text("other", encoding="utf-8")
                selected.write_text("new", encoding="utf-8")
                while len(events) < 2:
                    events.append(await asyncio.wait_for(watcher.__anext__(), timeout=1.0))
            return events

        events = asyncio.run(run())
        self.assertEqual({Path(event.src_path) for event in events}, {selected, other})

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
