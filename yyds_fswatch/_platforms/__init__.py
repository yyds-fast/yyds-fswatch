# -*- coding: utf-8 -*-

import sys
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from ._base import BaseWatcher
    from ..watcher import FSWatcher

def get_platform_watcher_class():
    if sys.platform.startswith("linux"):
        try:
            from ._linux import LinuxWatcher
            return LinuxWatcher
        except Exception:
            # Fallback to polling if inotify fails
            pass
    elif sys.platform == "win32":
        try:
            from ._windows import WindowsWatcher
            return WindowsWatcher
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            from ._darwin import DarwinWatcher
            return DarwinWatcher
        except Exception:
            pass
            
    from ._polling import PollingWatcher
    return PollingWatcher
