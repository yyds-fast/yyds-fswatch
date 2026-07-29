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

    async def astart(self):
        """Start the backend from an asyncio context.

        Native backends start synchronously by default. Backends with blocking
        initialization may override this method to keep the caller's loop
        responsive.
        """
        self.start()

    def stop(self):
        raise NotImplementedError

    async def aclose(self):
        """Asynchronously release backend resources.

        Native backends can normally stop synchronously. Backends that own
        blocking worker operations may override this method so callers do not
        block the asyncio loop while waiting for those workers to finish.
        """
        self.stop()

    @property
    def is_running(self) -> bool:
        return False

    def is_healthy_for(self, paths: List[Path]) -> bool:
        """Whether this backend still covers every currently existing root."""
        return self.is_running
