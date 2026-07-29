# -*- coding: utf-8 -*-

import unittest
import tempfile
import shutil
import time
import gc
import weakref
import asyncio
from pathlib import Path
from yyds_fswatch import FSWatcher
from yyds_fswatch.ignore import GitIgnoreFilter

class TestFSWatcherIndustrial(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(dir=Path(".").resolve())).resolve()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_nested_gitignore(self):
        # Create a nested directory structure
        sub_dir = self.test_dir / "src"
        sub_dir.mkdir()
        
        # Create nested gitignore file
        nested_gitignore = sub_dir / ".gitignore"
        with open(nested_gitignore, "w", encoding="utf-8") as f:
            f.write("*.log\n/ignored_file.txt\n")

        filt = GitIgnoreFilter(
            root_paths=[self.test_dir],
            use_gitignore=True,
            ignore_patterns=None
        )

        # 1. *.log inside sub_dir should be ignored
        self.assertTrue(filt.is_ignored(sub_dir / "debug.log"))
        
        # 2. *.log outside sub_dir should NOT be ignored
        self.assertFalse(filt.is_ignored(self.test_dir / "debug.log"))

        # 3. /ignored_file.txt inside sub_dir should be ignored
        self.assertTrue(filt.is_ignored(sub_dir / "ignored_file.txt"))

        # 4. /ignored_file.txt outside sub_dir should NOT be ignored
        self.assertFalse(filt.is_ignored(self.test_dir / "ignored_file.txt"))
        
        filt.close()

    def test_memory_leak_prevention(self):
        filt = GitIgnoreFilter(
            root_paths=[self.test_dir],
            use_gitignore=False
        )
        # Create a weak reference to check if the filter object is garbage collected
        ref = weakref.ref(filt)
        
        # Close to break the lru_cache circular reference
        filt.close()
        del filt
        
        # Force garbage collection
        gc.collect()
        
        # Verify the object was collected and reference is None
        self.assertIsNone(ref())

    def test_overflow_bypass_debounce(self):
        watcher = FSWatcher(self.test_dir, debounce=5.0)
        events = []

        async def run_test():
            async with watcher as w:
                # 1. Trigger raw event (modified)
                watcher._on_raw_event("modified", self.test_dir / "test.txt", is_dir=False)
                
                # 2. Trigger overflow raw event
                watcher._on_raw_event("overflow", self.test_dir, is_dir=False)
                
                # 3. Try to read events with a short timeout.
                # If debounce is bypassed, they should be available immediately
                start_time = time.time()
                while len(events) < 2:
                    if time.time() - start_time > 1.0:
                        break
                    try:
                        event = await asyncio.wait_for(w.__anext__(), timeout=0.1)
                        events.append(event)
                    except asyncio.TimeoutError:
                        continue
                    except StopAsyncIteration:
                        break

        asyncio.run(run_test())

        # Check that we received both the modified event (flushed by overflow) and the overflow event itself
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, "modified")
        self.assertEqual(events[1].event_type, "overflow")

    def test_overflow_refreshes_gitignore_rules(self):
        target = self.test_dir / "newly-ignored.log"
        gitignore = self.test_dir / ".gitignore"

        async def run_test():
            async with FSWatcher(
                self.test_dir,
                use_gitignore=True,
                ignore_common_dirs=False,
            ) as watcher:
                self.assertFalse(watcher._ignore_filter.is_ignored(target))
                gitignore.write_text("*.log\n", encoding="utf-8")
                watcher._push_to_debounce("overflow", self.test_dir, False)
                return watcher._ignore_filter.is_ignored(target)

        self.assertTrue(asyncio.run(run_test()))

    def test_root_recovery(self):
        watcher = FSWatcher(self.test_dir, debounce=0.05, recovery_interval=0.05)
        events = []

        def perform_root_operations():
            # Wait for watcher to initialize
            for _ in range(20):
                if watcher.is_ready:
                    break
                time.sleep(0.1)
            time.sleep(0.1)

            # 1. Delete root directory
            shutil.rmtree(self.test_dir, ignore_errors=True)
            time.sleep(0.15) # Wait for recovery loop to detect missing path and stop watcher

            # 2. Recreate root directory
            self.test_dir.mkdir(exist_ok=True)
            time.sleep(0.15) # Wait for recovery loop to detect recreated path and restart watcher

            # 3. Create a file inside recreated root directory
            (self.test_dir / "recovered.txt").touch()
            time.sleep(0.5)

        import threading
        t = threading.Thread(target=perform_root_operations)
        t.start()

        import queue
        from yyds_fswatch.watcher import _STOP_SENTINEL
        with watcher as w:
            start_time = time.time()
            while True:
                # Allow up to 10 seconds for the recovery cycle to complete
                if time.time() - start_time > 10.0:
                    break
                try:
                    event = w._sync_queue.get(timeout=0.1)
                    if event is _STOP_SENTINEL:
                        break
                    events.append(event)
                    # Once we catch the recovered.txt created event, we can stop
                    if event.event_type == "created" and "recovered.txt" in event.src_path:
                        break
                except queue.Empty:
                    continue

        t.join()

        # Check if we successfully recovered and received the "created" event
        created_events = [e for e in events if e.event_type == "created" and "recovered.txt" in e.src_path]
        self.assertTrue(len(created_events) > 0, f"Expected created event after recovery, got events: {events}")

if __name__ == "__main__":
    unittest.main()
