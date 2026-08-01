"""把 ffmpeg remux 子进程包装为可订阅的进度与结果流。"""

import os
import re
import shlex
from subprocess import PIPE, Popen
from typing import Any, Final, List, Optional, Union

import attr
from reactivex import Observable, abc, create
from reactivex.disposable import CompositeDisposable, Disposable, SerialDisposable
from reactivex.scheduler.currentthreadscheduler import CurrentThreadScheduler
from tqdm import tqdm

from blrec.path.helpers import video_path

__all__ = 'RemuxingResult', 'remux_video'


@attr.s(auto_attribs=True, slots=True, frozen=True)
class RemuxingProgress:
    count: int
    total: int


_ERROR_PATTERN = re.compile(
    r'\b(error|missing|invalid|corrupt|illegal|overflow|out of range)\b', re.IGNORECASE
)


@attr.s(auto_attribs=True, slots=True, frozen=True)
class RemuxingResult:
    """结合退出码和 stderr 关键字对 ffmpeg 结果做保守分类。"""

    return_code: int
    output: str

    def is_done(self) -> bool:
        return self.return_code == 0

    def is_successful(self) -> bool:
        return (
            self.is_done()
            and not self.may_timestamps_incorrect()
            and not self.has_errors()
        )

    def is_warned(self) -> bool:
        return self.is_done() and self.may_timestamps_incorrect()

    def is_failed(self) -> bool:
        return not self.is_done() or self.has_errors()

    def has_errors(self) -> bool:
        return _ERROR_PATTERN.search(self.output) is not None

    def may_timestamps_incorrect(self) -> bool:
        return 'Non-monotonous DTS in output stream' in self.output


def remux_video(
    in_path: str,
    out_path: str,
    metadata_path: Optional[str] = None,
    *,
    display_progress: bool = False,
    remove_filler_data: bool = False,
) -> Observable[Union[RemuxingProgress, RemuxingResult]]:
    # ffmpeg 只在 stderr 状态行报告当前输出大小，以此近似 remux 进度。
    SIZE_PATTERN: Final = re.compile(r'size=\s*(?P<number>\d+)(?P<unit>[a-zA-Z]?B)')
    if in_path.endswith('.m3u8'):
        _in_path = video_path(in_path)
        total = os.path.getsize(_in_path)
        postfix = os.path.basename(_in_path)
    else:
        total = os.path.getsize(in_path)
        postfix = os.path.basename(in_path)

    def parse_size(line: str) -> int:
        match = SIZE_PATTERN.search(line)
        assert match is not None
        result = match.groupdict()

        unit = result['unit']
        number = int(result['number'])

        if unit == 'B':
            size = number
        elif unit == 'kB':
            size = 1024 * number
        elif unit == 'MB':
            size = 1024**2 * number
        elif unit == 'GB':
            size = 1024**3 * number
        else:
            raise ValueError(unit)

        return size

    def should_output_line(line: str) -> bool:
        line = line.strip()
        return not (
            line.startswith('frame=')
            or line.startswith('Press [q]')
            or (line.startswith('[hls') and 'Opening' in line and 'for reading' in line)
        )

    def subscribe(
        observer: abc.ObserverBase[Union[RemuxingProgress, RemuxingResult]],
        scheduler: Optional[abc.SchedulerBase] = None,
    ) -> abc.DisposableBase:
        _scheduler = scheduler or CurrentThreadScheduler.singleton()

        disposed = False
        cancelable = SerialDisposable()

        def action(scheduler: abc.SchedulerBase, state: Optional[Any] = None) -> None:
            if disposed:
                return

            with tqdm(
                desc='Remuxing',
                total=total,
                unit='B',
                unit_scale=True,
                unit_divisor=1024,
                postfix=postfix,
                disable=not display_progress,
            ) as pbar:
                cmd = f'ffmpeg -i "{in_path}"'
                if metadata_path is not None:
                    cmd += f' -i "{metadata_path}" -map_metadata 1'
                cmd += ' -codec copy'
                if remove_filler_data:
                    # 移除 H.264 NAL type 12 filler data，避免纯封装复制保留无效填充。
                    # 背景：https://forum.doom9.org/showthread.php?t=152051
                    # ISO/IEC 14496-10:2020(E)
                    # Table 7-1 – NAL unit type codes, syntax element categories, and NAL unit type classes  # noqa
                    # 7.4.2.7 Filler data RBSP semantics
                    cmd += ' -bsf:v filter_units=remove_types=12'
                cmd += f' "{out_path}" -y'

                args = shlex.split(cmd)
                out_lines: List[str] = []

                try:
                    with Popen(
                        args, stderr=PIPE, encoding='utf8', errors='backslashreplace'
                    ) as process:
                        assert process.stderr is not None
                        while not disposed:
                            line = process.stderr.readline()
                            if not line:
                                if process.poll() is not None:
                                    break
                                else:
                                    continue

                            try:
                                size = parse_size(line)
                            except Exception:
                                pass
                            else:
                                pbar.update(size - pbar.n)
                                progress = RemuxingProgress(size, total)
                                observer.on_next(progress)

                            if should_output_line(line):
                                out_lines.append(line)

                        if not disposed and process.returncode == 0:
                            pbar.update(total)
                            progress = RemuxingProgress(total, total)
                            observer.on_next(progress)
                except Exception as e:
                    observer.on_error(e)
                else:
                    result = RemuxingResult(process.returncode, ''.join(out_lines))
                    observer.on_next(result)
                    observer.on_completed()

        cancelable.disposable = _scheduler.schedule(action)

        def dispose() -> None:
            # dispose 只停止读取进度；Popen 上下文退出时负责等待/关闭子进程资源。
            nonlocal disposed
            disposed = True

        return CompositeDisposable(cancelable, Disposable(dispose))

    return create(subscribe)
