# -*- coding: utf-8 -*-

from dataclasses import dataclass

@dataclass
class FSEvent:
    event_type: str         # 'created' | 'modified' | 'deleted' | 'moved' | 'overflow'
    src_path: str           # absolute path of source (empty string or root directory for 'overflow')
    dest_path: str = ""     # absolute path of destination (only for 'moved')
    is_directory: bool = False

    def __repr__(self) -> str:
        dir_str = " (dir)" if self.is_directory else ""
        if self.event_type == "moved":
            return f"<FSEvent event_type={self.event_type!r} src_path={self.src_path!r} dest_path={self.dest_path!r}{dir_str}>"
        return f"<FSEvent event_type={self.event_type!r} src_path={self.src_path!r}{dir_str}>"
