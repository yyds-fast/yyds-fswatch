# -*- coding: utf-8 -*-

from .__version__ import __version__, __title__, __author__, __license__
from .events import FSEvent
from .watcher import FSWatcher, watch

__all__ = [
    "FSWatcher",
    "FSEvent",
    "watch",
    "__version__",
    "__title__",
    "__author__",
    "__license__",
]
