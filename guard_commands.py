"""从 AstrBot handler 中提取“本次消息命中的指令名”。

本模块只做 duck typing，不导入 astrbot：传入的 handler 只需要具备
``event_filters`` / ``enabled`` 等属性即可。这样既方便单元测试，
也让插件在 AstrBot 版本变动时更容易降级。

匹配语义与 AstrBot v4.25 的 ``CommandFilter`` / ``CommandGroupFilter`` 对齐：
- 指令名取 ``get_complete_command_names()``，其中已包含别名与父指令组前缀，
  并且 WebUI 改名后该方法会重算，因此守卫无需缓存指令名；
- 消息侧先折叠空白，再判断“完全相等”或“以 ``指令名 + 空格`` 开头”。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_WHITESPACE_RE = re.compile(r"\s+")


def _filter_kind(event_filter: Any) -> str:
    """返回过滤器类别名，避免直接依赖 astrbot 的类对象。"""
    return type(event_filter).__name__


def collapse_whitespace(text: str) -> str:
    """折叠连续空白，与 AstrBot 指令过滤器的归一化方式一致。"""
    return _WHITESPACE_RE.sub(" ", (text or "").strip())


def _is_command_filter(event_filter: Any) -> bool:
    """判断过滤器是否是指令（组）过滤器。

    优先按类名判断（AstrBot 的 ``CommandFilter`` / ``CommandGroupFilter``），
    同时保留属性兜底，避免子类化或改名导致守卫失效。
    """
    if _filter_kind(event_filter) in ("CommandFilter", "CommandGroupFilter"):
        return True
    if callable(getattr(event_filter, "get_complete_command_names", None)):
        return True
    return hasattr(event_filter, "command_name") or hasattr(event_filter, "group_name")


def _command_names_from_filter(event_filter: Any) -> list[str]:
    """从单个 CommandFilter / CommandGroupFilter 中取出全部完整指令名。"""
    getter = getattr(event_filter, "get_complete_command_names", None)
    if callable(getter):
        try:
            names = getter()
        except Exception:
            names = None
        if isinstance(names, Iterable) and not isinstance(names, (str, bytes)):
            collected = [str(name).strip() for name in names if str(name).strip()]
            if collected:
                return collected

    # 兜底：直接读属性（旧版本或自定义实现可能没有 get_complete_command_names）
    for attr in ("command_name", "group_name"):
        value = getattr(event_filter, attr, None)
        if isinstance(value, str) and value.strip():
            names = [value.strip()]
            aliases = getattr(event_filter, "alias", None)
            if isinstance(aliases, (set, list, tuple)):
                names.extend(
                    str(alias).strip() for alias in aliases if str(alias).strip()
                )
            parent_names = getattr(event_filter, "parent_command_names", None)
            if isinstance(parent_names, (list, tuple)) and parent_names:
                parent = str(parent_names[0] or "").strip()
                if parent:
                    names = [f"{parent} {name}" for name in names]
            return names
    return []


def handler_command_names(handler: Any) -> list[str]:
    """取出一个 handler 注册的全部完整指令名（含别名与父指令组前缀）。"""
    names: list[str] = []
    seen: set[str] = set()
    for event_filter in getattr(handler, "event_filters", None) or []:
        if not _is_command_filter(event_filter):
            continue
        for name in _command_names_from_filter(event_filter):
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def is_handler_enabled(handler: Any) -> bool:
    """handler 是否处于启用状态（禁用指令不参与守卫判定）。"""
    enabled = getattr(handler, "enabled", True)
    return bool(enabled)


def match_message_commands(message_str: str, command_names: Iterable[str]) -> list[str]:
    """返回本次消息命中的指令名列表（按传入顺序）。

    命中条件：消息与指令名完全相等，或以 ``指令名 + 空格`` 开头。
    因此 ``群管`` 会命中 ``群管 禁言``，而 ``帮助x`` 不会命中 ``帮助``。
    """
    text = collapse_whitespace(message_str)
    if not text:
        return []
    matched: list[str] = []
    for name in command_names:
        normalized = collapse_whitespace(name)
        if not normalized:
            continue
        if text == normalized or text.startswith(normalized + " "):
            matched.append(normalized)
    return matched


def matched_commands_for_event(
    event: Any,
    handlers: Iterable[Any],
) -> list[str]:
    """遍历 handlers，返回本次消息命中的全部指令名（去重、保持顺序）。"""
    message_str = ""
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            message_str = str(getter() or "")
        except Exception:
            message_str = ""
    if not message_str:
        message_str = str(getattr(event, "message_str", "") or "")

    matched: list[str] = []
    seen: set[str] = set()
    for handler in handlers:
        if not is_handler_enabled(handler):
            continue
        names = handler_command_names(handler)
        if not names:
            continue
        for name in match_message_commands(message_str, names):
            if name not in seen:
                seen.add(name)
                matched.append(name)
    return matched
