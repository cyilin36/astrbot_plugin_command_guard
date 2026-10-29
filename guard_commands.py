"""事件解析工具：唤醒方式分类、匹配文本提取与指令 handler 识别。

本模块只做 duck typing，不导入 astrbot：传入的 event / handler 只需要具备
``get_messages`` / ``message_obj`` / ``event_filters`` 等属性即可。这样既方便
单元测试，也让插件在 AstrBot 版本变动时更容易降级。

匹配语义与 AstrBot v4.25 的 ``WakingCheckStage`` 对齐：
- 唤醒词路径由 ``wake_prefix`` 前缀触发，框架会改写 ``event.message_str``，
  但 ``message_obj.message_str`` 仍保留原始文本，因此可以据此区分唤醒方式；
- 直接 @bot / 回复 bot / 私聊不视为唤醒词，不受「忽略非管理员唤醒词」影响。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

try:  # 包内导入（AstrBot 运行时）
    from .guard_policy import WAKE_DIRECT, WAKE_OTHER, WAKE_PREFIX, WAKE_PRIVATE
except ImportError:  # 纯逻辑测试时以顶层模块导入
    from guard_policy import WAKE_DIRECT, WAKE_OTHER, WAKE_PREFIX, WAKE_PRIVATE

_WHITESPACE_RE = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    """折叠连续空白，与 AstrBot 指令过滤器的归一化方式一致。"""
    return _WHITESPACE_RE.sub(" ", (text or "").strip())


def _filter_kind(event_filter: Any) -> str:
    """返回过滤器类别名，避免直接依赖 astrbot 的类对象。"""
    return type(event_filter).__name__


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
    """handler 是否处于启用状态（禁用 handler 不参与注入）。"""
    enabled = getattr(handler, "enabled", True)
    return bool(enabled)


# ---------------------------------------------------------------- 唤醒方式


def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    """容错的 getattr：事件对象异常时返回默认值，而不是把守卫自己炸掉。"""
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _get_messages(event: Any) -> list[Any]:
    """取出消息链，兼容 ``get_messages()`` 与 ``message_obj.message``。"""
    getter = _safe_getattr(event, "get_messages", None)
    if callable(getter):
        try:
            messages = getter()
        except Exception:
            messages = None
        if isinstance(messages, Iterable) and not isinstance(messages, (str, bytes)):
            return list(messages)
    obj = _safe_getattr(event, "message_obj", None)
    messages = _safe_getattr(obj, "message", None)
    if isinstance(messages, Iterable) and not isinstance(messages, (str, bytes)):
        return list(messages)
    return []


def _component_kind(component: Any) -> str:
    """返回消息段类别名，避免直接依赖 astrbot 的类对象。"""
    return type(component).__name__


def _get_self_id(event: Any) -> str:
    getter = _safe_getattr(event, "get_self_id", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            return ""
    return ""


def _is_private_chat(event: Any) -> bool:
    getter = _safe_getattr(event, "is_private_chat", None)
    if callable(getter):
        try:
            return bool(getter())
        except Exception:
            return False
    return False


def _raw_message_str(event: Any) -> str:
    """取唤醒检查前的原始文本（保留唤醒前缀）。

    AstrBot 的 ``WakingCheckStage`` 只会改写 ``event.message_str``，不会动
    ``message_obj.message_str``，因此后者可以可靠地区分「唤醒词」与「@bot」。
    """
    obj = _safe_getattr(event, "message_obj", None)
    if obj is not None:
        raw = _safe_getattr(obj, "message_str", None)
        if isinstance(raw, str) and raw:
            return raw
    raw = _safe_getattr(event, "message_str", "")
    return raw if isinstance(raw, str) else ""


def classify_wake_kind(
    event: Any,
    wake_prefixes: Iterable[str] = ("/",),
) -> str:
    """判断本次已唤醒事件的唤醒方式。

    - ``direct``：At 自己或 Reply 自己（直接 @bot / 回复 bot）；
    - ``private``：私聊；
    - ``prefix``：原始消息以唤醒前缀开头；
    - ``other``：其余已唤醒消息（@全体、平台默认唤醒等）。
    """
    self_id = _get_self_id(event)
    for component in _get_messages(event):
        kind = _component_kind(component)
        if kind in ("At", "AtAll"):
            if self_id and str(getattr(component, "qq", "")) == self_id:
                return WAKE_DIRECT
        elif kind == "Reply":
            if self_id and str(getattr(component, "sender_id", "")) == self_id:
                return WAKE_DIRECT

    if _is_private_chat(event):
        return WAKE_PRIVATE

    raw = _raw_message_str(event).lstrip()
    if raw:
        prefixes = [
            prefix
            for prefix in (wake_prefixes or ())
            if isinstance(prefix, str) and prefix
        ]
        if any(raw.startswith(prefix) for prefix in prefixes):
            return WAKE_PREFIX

    return WAKE_OTHER


# -------------------------------------------------------------- 匹配文本


def message_text_for_matching(event: Any) -> str:
    """提取用于屏蔽词匹配的文本。

    优先拼接消息链里的 ``Plain`` 文本段：AstrBot 的 ``message_str`` 会包含
    ``@其他用户`` 占位符，直接用它做前缀匹配会被「先 @ 别人再 @bot」绕过。
    没有 Plain 文本段时回退到 ``get_message_str()``，再回退到 ``message_str``
    属性；任何异常都静默回退。
    """
    parts: list[str] = []
    try:
        for component in _get_messages(event):
            if _component_kind(component) != "Plain":
                continue
            text = _safe_getattr(component, "text", "")
            if isinstance(text, str):
                parts.append(text)
    except Exception:
        parts = []
    if parts and any(part.strip() for part in parts):
        return "".join(parts)

    getter = _safe_getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            text = getter()
        except Exception:
            text = ""
        if isinstance(text, str) and text:
            return text

    text = _safe_getattr(event, "message_str", "")
    return text if isinstance(text, str) else ""


__all__ = [
    "classify_wake_kind",
    "collapse_whitespace",
    "handler_command_names",
    "is_handler_enabled",
    "message_text_for_matching",
]
