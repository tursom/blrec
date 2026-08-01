"""AVC Exp-Golomb 等位级语法使用的只读游标。"""

from __future__ import annotations

from bitarray import bitarray
from bitarray.util import ba2int


__all__ = 'BitsReader',


class BitsReader:
    """从 bitarray 顺序读取位段；越界切片由 bitarray 返回实际剩余位。"""

    def __init__(self, bits: bitarray) -> None:
        self._bits = bits
        self._ptr = 0

    def read_bits_as_int(self, n: int) -> int:
        return ba2int(self.read_bits(n))

    def read_bits(self, n: int) -> bitarray:
        result = self.next_bits(n)
        self._ptr += n
        return result

    def next_bits(self, n: int) -> bitarray:
        assert n >= 0
        if n == 0:
            # ba2int 不接受空 bitarray；用单个零位保证 read_bits_as_int(0) 返回 0，游标仍不前移。
            return bitarray('0')
        return self._bits[self._ptr:self._ptr + n]
