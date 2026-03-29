import os
import re
from datetime import datetime
from typing import Tuple

from blrec.bili.live import Live
from blrec.path import escape_path
from blrec.utils.mixins import AsyncCooperationMixin

__all__ = ('PathProvider',)


class PathProvider(AsyncCooperationMixin):
    def __init__(self, live: Live, out_dir: str, path_template: str) -> None:
        super().__init__()
        self._live = live
        self.out_dir = out_dir
        self.path_template = path_template

    def __call__(self, timestamp: int = None) -> Tuple[str, int]:
        if timestamp is None:
            timestamp = self._call_coroutine(self._live.get_timestamp())
        path = self._make_unique_path(self._make_path(timestamp, '.flv'))
        return path, timestamp

    def make_raw_danmaku_path(
        self, timestamp: int = None, *, suffix: str = '.jsonl'
    ) -> Tuple[str, int]:
        if timestamp is None:
            timestamp = self._call_coroutine(self._live.get_timestamp())
        path = self._make_unique_path(self._make_path(timestamp, suffix))
        return path, timestamp

    def _make_path(self, timestamp: int, suffix: str) -> str:
        date_time = datetime.fromtimestamp(timestamp)
        relpath = self.path_template.format(
            roomid=self._live.room_id,
            uname=escape_path(self._live.user_info.name),
            title=escape_path(self._live.room_info.title),
            area=escape_path(self._live.room_info.area_name),
            parent_area=escape_path(self._live.room_info.parent_area_name),
            year=date_time.year,
            month=str(date_time.month).rjust(2, '0'),
            day=str(date_time.day).rjust(2, '0'),
            hour=str(date_time.hour).rjust(2, '0'),
            minute=str(date_time.minute).rjust(2, '0'),
            second=str(date_time.second).rjust(2, '0'),
        )

        pathname = os.path.abspath(os.path.expanduser(os.path.join(self.out_dir, relpath)))
        pathname += suffix
        os.makedirs(os.path.dirname(pathname), exist_ok=True)
        return pathname

    def _make_unique_path(self, pathname: str) -> str:
        while os.path.exists(pathname):
            root, ext = os.path.splitext(pathname)
            m = re.search(r'_\((\d+)\)$', root)
            if m is None:
                root += '_(1)'
            else:
                root = re.sub(r'\(\d+\)$', f'({int(m.group(1)) + 1})', root)
            pathname = root + ext

        return pathname
