# -*- coding: utf-8 -*-

import unittest
import tempfile
import shutil
import time
import os
from pathlib import Path
from yyds_fswatch import FSWatcher

class TestFSWatcherFileWatch(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(dir=Path(".").resolve())).resolve()
        self.target_file = self.test_dir / "target.txt"
        self.other_file = self.test_dir / "other.txt"
        
        # Pre-create files
        self.target_file.touch()
        self.other_file.touch()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_single_file_watch_isolation(self):
        # Monitor ONLY the target file
        watcher = FSWatcher(self.target_file, debounce=0.05)
        events = []

        def perform_changes():
            # Wait for watcher to start
            for _ in range(20):
                if watcher.is_ready:
                    break
                time.sleep(0.1)
            time.sleep(0.1)

            # 1. Modify other file (should NOT be detected)
            with open(self.other_file, "a") as f:
                f.write("change\n")
            time.sleep(0.2)

            # 2. Modify target file (should be detected)
            with open(self.target_file, "a") as f:
                f.write("target change\n")
            time.sleep(0.2)

            # 3. Simulate Atomic Save/Replace on target file (delete + recreate)
            # This is how VS Code/Vim save files.
            self.target_file.unlink()
            time.sleep(0.1)
            self.target_file.touch()
            time.sleep(0.2)

            # 4. Delete target file
            self.target_file.unlink()
            time.sleep(0.2)

        import threading
        t = threading.Thread(target=perform_changes)
        t.start()

        import queue
        from yyds_fswatch.watcher import _STOP_SENTINEL
        with watcher as w:
            start_time = time.time()
            while True:
                if time.time() - start_time > 5.0:
                    break
                try:
                    event = w._sync_queue.get(timeout=0.1)
                    if event is _STOP_SENTINEL:
                        break
                    events.append(event)
                except queue.Empty:
                    continue

        t.join()

        # We expect only target_file events:
        # - modified (step 2)
        # - deleted (step 3 delete)
        # - created (step 3 recreate)
        # - deleted (step 4 delete)
        # Total events >= 4, and NO events should reference other.txt!
        self.assertTrue(len(events) >= 4, f"Expected >= 4 events, got {len(events)}: {events}")
        
        for e in events:
            self.assertIn("target.txt", e.src_path)
            self.assertNotIn("other.txt", e.src_path)

if __name__ == "__main__":
    unittest.main()
