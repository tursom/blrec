"""复制弹幕时重算相对时间轴的共享逻辑。"""


from typing import Optional

import attr

from .io import DanmakuReader, DanmakuWriter


async def copy_damus(
    reader: DanmakuReader,
    writer: DanmakuWriter,
    *,
    timebase: Optional[int] = None,  # milliseconds
    delta: int = 0,  # milliseconds
) -> None:
    async for danmu in reader.read_danmus():
        # date 是毫秒级墙钟时间，stime 是播放器使用的片内秒数。
        if timebase is None:
            stime = max(0, danmu.stime * 1000 + delta) / 1000
        else:
            stime = max(0, danmu.date - timebase + delta) / 1000
        new_danmu = attr.evolve(danmu, stime=stime)
        await writer.write_danmu(new_danmu)
