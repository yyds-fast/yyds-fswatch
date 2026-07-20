# -*- coding: utf-8 -*-

import unittest
import tempfile
import shutil
import time
from pathlib import Path
from yyds_fswatch import FSWatcher

class TestFSWatcher(unittest.TestCase):
    def setUp(self):
        # Create temp dir within workspace
        self.test_dir = Path(tempfile.mkdtemp(dir=Path(".").resolve())).resolve()

    def tearDown(self):
        # Remove temp dir
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sync_iterator_and_debounce(self):
        watcher = FSWatcher(self.test_dir, debounce=0.1)
        
        events = []
        
        # Start a helper to do file changes while watching
        def perform_changes():
            # Wait up to 2 seconds for watcher to start
            for _ in range(20):
                if watcher.is_ready:
                    break
                time.sleep(0.1)
            time.sleep(0.1)  # Extra buffer to ensure loop is active
            
            file_path = self.test_dir / "test.txt"
            
            # 1. Create file
            file_path.touch()
            time.sleep(0.2)  # Allow create to flush
            
            # 2. Burst of modifications (should be debounced to 1 modified event)
            for i in range(5):
                with open(file_path, "a") as f:
                    f.write(f"line {i}\n")
                time.sleep(0.01)
            time.sleep(0.2)  # Allow modified to flush
                
            # 3. Rename file
            new_path = self.test_dir / "test_new.txt"
            file_path.rename(new_path)
            
            # 4. Delete file
            time.sleep(0.2) # Allow rename to flush
            new_path.unlink()
            
            # Allow time to flush delete event
            time.sleep(0.2)

        # Run changes in a separate thread
        import threading
        t = threading.Thread(target=perform_changes)
        t.start()

        # Iterate over watcher events with timeout protection
        import queue
        from yyds_fswatch.watcher import _STOP_SENTINEL
        with watcher as w:
            start_time = time.time()
            while len(events) < 4:
                if time.time() - start_time > 5.0:
                    break
                try:
                    event = w._sync_queue.get(timeout=0.2)
                    if event is _STOP_SENTINEL:
                        break
                    events.append(event)
                except queue.Empty:
                    continue

        t.join()

        self.assertTrue(len(events) >= 4, f"Expected at least 4 events, got {len(events)}: {events}")
        
        # Check event types in order
        event_types = [e.event_type for e in events]
        self.assertEqual(event_types[0], "created")
        self.assertEqual(event_types[1], "modified")
        self.assertEqual(event_types[2], "moved")
        self.assertEqual(event_types[3], "deleted")
        
        # Verify moved event details
        moved_event = events[2]
        self.assertEqual(moved_event.event_type, "moved")
        self.assertEqual(Path(moved_event.src_path).name, "test.txt")
        self.assertEqual(Path(moved_event.dest_path).name, "test_new.txt")

    def test_decorator_mode(self):
        watcher = FSWatcher(self.test_dir, debounce=0.05)
        captured = []

        @watcher.on("created")
        def on_created(event):
            captured.append(event)

        @watcher.on("*")
        def on_any(event):
            captured.append(event)

        # Run decorator start in a thread since it is blocking
        import threading
        t_watcher = threading.Thread(target=watcher.start)
        t_watcher.daemon = True
        t_watcher.start()

        # Wait up to 2 seconds for loop to be initialized in the thread
        for _ in range(20):
            if watcher.is_ready:
                break
            time.sleep(0.1)
        time.sleep(0.1)  # Extra buffer to ensure loop is active

        (self.test_dir / "decor.txt").touch()
        time.sleep(0.2)  # Wait for event dispatch

        watcher.stop()
            
        t_watcher.join(timeout=2.0)
        self.assertFalse(t_watcher.is_alive())

        # We touched decor.txt. We expect:
        # 1. on_created called (1 event)
        # 2. on_any called (1 event)
        # Total events in captured >= 2
        self.assertTrue(len(captured) >= 2)
        event_types = [e.event_type for e in captured]
        self.assertIn("created", event_types)

if __name__ == "__main__":
    unittest.main()
