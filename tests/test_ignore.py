# -*- coding: utf-8 -*-

import unittest
from pathlib import Path
from yyds_fswatch.ignore import GitIgnoreFilter

class TestGitIgnoreFilter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(".").resolve()

    def test_basic_matching(self):
        # We simulate GitIgnoreFilter with root self.temp_dir
        # We pass patterns directly
        filt = GitIgnoreFilter(
            root_paths=[self.temp_dir],
            use_gitignore=False,
            ignore_patterns=[
                "*.log",
                "build/",
                "/dist",
                "!important.log"
            ]
        )

        # 1. *.log file anywhere should be ignored
        self.assertTrue(filt.is_ignored(self.temp_dir / "error.log"))
        self.assertTrue(filt.is_ignored(self.temp_dir / "src" / "debug.log"))

        # 2. important.log should NOT be ignored (negation)
        self.assertFalse(filt.is_ignored(self.temp_dir / "important.log"))
        self.assertFalse(filt.is_ignored(self.temp_dir / "src" / "important.log"))

        # 3. build/ directory only should be ignored
        self.assertTrue(filt.is_ignored(self.temp_dir / "build", is_dir=True))
        self.assertTrue(filt.is_ignored(self.temp_dir / "src" / "build", is_dir=True))
        
        # 4. build file should NOT be ignored because build/ is directory-only
        self.assertFalse(filt.is_ignored(self.temp_dir / "build", is_dir=False))

        # 5. /dist should only be ignored at root
        self.assertTrue(filt.is_ignored(self.temp_dir / "dist"))
        self.assertFalse(filt.is_ignored(self.temp_dir / "src" / "dist"))

    def test_fast_ignore_dirs(self):
        filt = GitIgnoreFilter(root_paths=[self.temp_dir], use_gitignore=False)
        self.assertTrue(filt.is_ignored(self.temp_dir / "node_modules" / "express" / "index.js"))
        self.assertTrue(filt.is_ignored(self.temp_dir / "src" / ".git" / "config"))
        self.assertTrue(filt.is_ignored(self.temp_dir / "__pycache__" / "watcher.cpython-310.pyc"))

    def test_lru_cache(self):
        filt = GitIgnoreFilter(root_paths=[self.temp_dir], use_gitignore=False, ignore_patterns=["*.tmp"])
        path = self.temp_dir / "test.tmp"
        
        # First call, uncached
        self.assertTrue(filt.is_ignored(path))
        # Second call, cached
        self.assertTrue(filt.is_ignored(path))
        
        # Verify hit info exists in cache
        info = filt.is_ignored.cache_info()
        self.assertEqual(info.hits, 1)
        self.assertEqual(info.misses, 1)

if __name__ == "__main__":
    unittest.main()
