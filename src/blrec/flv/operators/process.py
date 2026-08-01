"""按固定顺序组合 FLV 清理、切分、校时与断流拼接操作符。"""

from typing import Callable

from reactivex import operators as ops

from ..common import is_avc_end_sequence_tag
from .concat import concat
from .correct import correct
from .defragment import defragment
from .fix import fix
from .sort import sort
from .split import split
from .typing import FLVStream

__all__ = ('process',)


def process(sort_tags: bool = False) -> Callable[[FLVStream], FLVStream]:
    """构建标准处理链；低延迟模式可跳过额外排序。"""

    def _process(source: FLVStream) -> FLVStream:
        if sort_tags:
            return source.pipe(
                defragment(),
                split(),
                sort(),
                ops.filter(lambda v: not is_avc_end_sequence_tag(v)),  # type: ignore
                correct(),
                fix(),
                concat(),
            )
        else:
            return source.pipe(
                defragment(),
                split(),
                ops.filter(lambda v: not is_avc_end_sequence_tag(v)),  # type: ignore
                correct(),
                fix(),
                concat(),
            )

    return _process
