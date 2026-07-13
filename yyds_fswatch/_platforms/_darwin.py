# -*- coding: utf-8 -*-
import sys
if sys.platform != "darwin":
    raise ImportError("macOS only")

from ._base import BaseWatcher

class DarwinWatcher(BaseWatcher):
    def start(self):
        raise NotImplementedError("macOS support is in Phase 3")

    def stop(self):
        pass
