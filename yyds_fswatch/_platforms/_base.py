# -*- coding: utf-8 -*-

import asyncio
from pathlib import Path
from typing import List, Callable

class BaseWatcher:
    def __init__(self, paths: List[Path], recursive: bool, loop: asyncio.AbstractEventLoop, on_raw_event: Callable):
        self.paths = paths
        self.recursive = recursive
        self.loop = loop
        self.on_raw_event = on_raw_event  # Signature: on_raw_event(event_type: str, path: Path, is_dir: bool, dest_path: Path = None, cookie: int = 0)

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    @property
    def is_running(self) -> bool:
        return False
