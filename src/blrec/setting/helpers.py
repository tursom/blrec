"""Pydantic 设置模型的递归合并工具。"""

from typing import TypeVar

from pydantic.main import BaseModel


__all__ = 'update_settings', 'shadow_settings'


_T = TypeVar('_T', bound=BaseModel)


def update_settings(src: _T, dst: _T) -> None:
    """把 PATCH 请求中显式出现的字段递归写入持久化设置。"""

    overwrite_settings(src, dst, exclude_unset=True)


def shadow_settings(src: _T, dst: _T) -> None:
    """用任务级非空字段覆盖一份全局设置副本，得到最终生效值。"""

    overwrite_settings(src, dst, exclude_none=True)


def overwrite_settings(
    src: _T, dst: _T, exclude_unset: bool = False, exclude_none: bool = False
) -> None:
    # 调用方必须传入同构模型；递归过程依赖同名字段具有相同的嵌套结构。
    assert isinstance(src, BaseModel) and isinstance(dst, BaseModel)

    # exclude_unset 区分“客户端未提交”与“客户端提交了字段的默认值”。
    fields = src.__fields_set__ if exclude_unset else src.__fields__

    for name in fields:
        if not hasattr(dst, name):
            continue
        value = getattr(src, name)
        if exclude_none and value is None:
            continue
        if not isinstance(value, BaseModel):
            setattr(dst, name, value)
        else:
            overwrite_settings(
                value,
                getattr(dst, name),
                exclude_unset=exclude_unset,
                exclude_none=exclude_none,
            )
