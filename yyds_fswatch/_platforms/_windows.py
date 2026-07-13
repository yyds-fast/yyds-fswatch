# -*- coding: utf-8 -*-
import sys
if sys.platform != "win32":
    raise ImportError("Windows only")

from ._base import BaseWatcher

class WindowsWatcher(BaseWatcher):
    def start(self):
        raise NotImplementedError("Windows support is in Phase 3")

    def stop(self):
        pass
