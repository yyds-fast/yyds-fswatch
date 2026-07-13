# -*- coding: utf-8 -*-

import re
from pathlib import Path
from typing import Union, List, Tuple
from functools import lru_cache

class GitIgnoreFilter:
    def __init__(self, root_paths: List[Path], use_gitignore: bool = True, ignore_patterns: List[str] = None):
        # We support multiple root paths, resolve them
        self._roots = [p.resolve() for p in root_paths]
        self._fast_ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".egg-info", "dist", "build"}
        self._compiled_rules = []

        # 1. Load from .gitignore files in the root paths
        if use_gitignore:
            for root in self._roots:
                gitignore_file = root / ".gitignore"
                if gitignore_file.is_file():
                    try:
                        self._parse_file(gitignore_file)
                    except Exception:
                        pass # Ignore reading errors

        # 2. Add extra custom ignore patterns
        if ignore_patterns:
            for pat in ignore_patterns:
                pat = pat.strip()
                if pat and not pat.startswith('#'):
                    self._add_rule(pat)

        # Bind the LRU cache to this instance to avoid memory leaks
        self.is_ignored = lru_cache(maxsize=4096)(self._is_ignored_impl)

    def _parse_file(self, path: Path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                self._add_rule(line)

    def _add_rule(self, pattern: str):
        is_negated = False
        if pattern.startswith('!'):
            is_negated = True
            pattern = pattern[1:]

        # Stripping leading/trailing spaces unless escaped is handled by basic stripping
        is_dir_only = pattern.endswith('/')
        if is_dir_only:
            pattern = pattern[:-1]

        anchored = False
        if pattern.startswith('/'):
            anchored = True
            pattern = pattern[1:]
        elif '/' in pattern:
            anchored = True

        regex_parts = []
        i = 0
        n = len(pattern)
        while i < n:
            c = pattern[i]
            if c == '*':
                if i + 1 < n and pattern[i+1] == '*':
                    # Double star **
                    # Check if surrounded by slashes
                    regex_parts.append('.*')
                    i += 2
                else:
                    regex_parts.append('[^/]*')
                    i += 1
            elif c == '?':
                regex_parts.append('[^/]')
                i += 1
            elif c in ('.', '+', '^', '$', '(', ')', '[', ']', '{', '}', '|', '\\'):
                regex_parts.append('\\' + c)
                i += 1
            else:
                regex_parts.append(c)
                i += 1

        regex_str = "".join(regex_parts)

        # Build regex matching
        if is_dir_only:
            suffix = '(/.*)?$'
        else:
            suffix = '(/.*)?$'

        if anchored:
            regex_str = '^' + regex_str + suffix
        else:
            regex_str = '(^|.*/)' + regex_str + suffix

        try:
            compiled = re.compile(regex_str)
            self._compiled_rules.append((compiled, is_dir_only, is_negated))
        except re.error:
            pass # Skip invalid regexes

    def _is_ignored_impl(self, path: Path, is_dir: bool = False) -> bool:
        resolved_path = path.resolve()
        
        # Determine the relative path to whichever root it belongs to
        rel_path = None
        for root in self._roots:
            try:
                rel_path = resolved_path.relative_to(root)
                break
            except ValueError:
                continue

        if rel_path is None:
            # If not under any watched root, we don't watch it (ignore it)
            return True

        # Fast path check for common directories
        for i, part in enumerate(rel_path.parts):
            if part in self._fast_ignore_dirs:
                if i < len(rel_path.parts) - 1 or is_dir:
                    return True

        # Evaluate rules in order (last matching rule wins)
        rel_path_str = str(rel_path).replace('\\', '/') # Normalize path separator for regex
        
        ignored = False
        for regex, is_dir_only, is_negated in self._compiled_rules:
            if is_dir_only and not is_dir:
                continue
            if regex.match(rel_path_str):
                ignored = not is_negated

        return ignored
