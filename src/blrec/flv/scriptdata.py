"""FLV ScriptData 的 AMF0 名称和值编码适配层。"""

from io import BytesIO
from typing import Any, BinaryIO, Mapping, TypedDict

from loguru import logger

from .amf import AMFReader, AMFWriter

__all__ = (
    'load',
    'loads',
    'dump',
    'dumps',
    'ScriptData',
    'ScriptDataParser',
    'ScriptDataDumper',
)


class ScriptData(TypedDict):
    name: str
    value: Any


class ScriptDataParser:
    """读取一个 AMF 名称和值；缺失或非法的 onMetaData 值降级为空对象。"""

    def __init__(self, stream: BinaryIO) -> None:
        self._reader = AMFReader(stream)

    def parse(self) -> ScriptData:
        name = self._parse_name()
        try:
            value = self._parse_value()
        except EOFError:
            logger.debug(f'No script data: {name}')
            value = {}
        if not isinstance(value, dict):
            if name == 'onMetaData':
                logger.debug(f'Invalid onMetaData: {value}')
                value = {}
            else:
                logger.debug(f'Unusual script data: {name}, {value}')
        return ScriptData(name=name, value=value)

    def _parse_name(self) -> str:
        value = self._reader.read_value()
        assert isinstance(value, str)
        return value

    def _parse_value(self) -> Any:
        return self._reader.read_value()


class ScriptDataDumper:
    """按 FLV ScriptData 规定的“事件名在前、对象值在后”顺序写入。"""

    def __init__(self, stream: BinaryIO) -> None:
        self._writer = AMFWriter(stream)

    def dump(self, script_data: ScriptData) -> None:
        self._dump_name(script_data['name'])
        self._dump_value(script_data['value'])

    def _dump_name(self, name: str) -> None:
        self._writer.write_value(name)

    def _dump_value(self, value: Mapping[str, Any]) -> None:
        self._writer.write_value(value)


def load(data: bytes) -> ScriptData:
    return loads(BytesIO(data))


def loads(stream: BinaryIO) -> ScriptData:
    return ScriptDataParser(stream).parse()


def dump(script_data: ScriptData) -> bytes:
    stream = BytesIO()
    dumps(script_data, stream)
    return stream.getvalue()


def dumps(script_data: ScriptData, stream: BinaryIO) -> None:
    ScriptDataDumper(stream).dump(script_data)
