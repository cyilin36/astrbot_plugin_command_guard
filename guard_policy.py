"""指令守卫的策略解析与判定逻辑。

本模块刻意不导入 astrbot：所有配置归一化与忽略判定都是纯函数，便于在没有
AstrBot 运行时的环境下直接做单元测试。

术语：
- 全局设置：插件配置里的 ``ignore_non_admin`` / ``ignored_commands`` /
  ``exempt_commands`` / ``hard_silence``；
- 唤醒方式：``direct``（@bot / 回复 bot）、``prefix``（唤醒词）、``private``
  （私聊）、``other``（@全体等平台唤醒）。只有 ``prefix`` 会被
  ``ignore_non_admin`` 拦截。

固定行为（不可配置）：管理员始终放行；机器人自身发出的事件始终放行；
被忽略的消息始终记录一条日志。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

DEFAULT_WAKE_PREFIXES: tuple[str, ...] = ("/",)

# 唤醒方式。只有 WAKE_PREFIX 会被「忽略非管理员唤醒词」开关拦截。
WAKE_DIRECT = "direct"
WAKE_PREFIX = "prefix"
WAKE_PRIVATE = "private"
WAKE_OTHER = "other"

_TRUE_TOKENS = frozenset({"on", "true", "1", "yes", "y", "enable", "enabled"})
_FALSE_TOKENS = frozenset({"off", "false", "0", "no", "n", "disable", "disabled"})

_WHITESPACE_RE = re.compile(r"\s+")
_COMMAND_LABEL_LIMIT = 48

# 判定原因（写日志时使用英文 token，便于 grep）
REASON_NOT_WOKEN = "not_woken"
REASON_ADMIN = "admin"
REASON_BOT_SELF = "bot_self"
REASON_EXEMPT = "exempt"
REASON_BLOCKED = "blocked_command"
REASON_NON_ADMIN_WAKE = "non_admin_wake"
REASON_ALLOWED = "allowed"


def _as_bool(value: Any, default: bool) -> bool:
    """把配置值解析为布尔；无法识别时回退到默认值。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    token = str(value).strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return default


def _as_str_list(value: Any) -> list[str]:
    """把配置值规整为字符串列表（支持逗号/换行分隔的单条字符串）。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[\n,，;；]", value)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, Iterable):
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    items.append(text)
        return items
    return []


def _collapse(text: str) -> str:
    """折叠空白，与 AstrBot 指令过滤器的归一化方式保持一致。"""
    return _WHITESPACE_RE.sub(" ", (text or "").strip())


def normalize_command_entry(raw: Any, wake_prefixes: Sequence[str] = DEFAULT_WAKE_PREFIXES) -> str:
    """归一化配置项 / 消息文本，用于比较。

    处理内容：折叠空白、去掉 AstrBot 唤醒前缀与字面 ``/``、统一大小写（casefold）。
    例如 ``/帮助``、``！ 帮助``、``帮助`` 都会归一化为 ``帮助``。
    """
    text = _collapse(str(raw or ""))
    if not text:
        return ""
    prefixes = {
        prefix.strip()
        for prefix in (wake_prefixes or DEFAULT_WAKE_PREFIXES)
        if isinstance(prefix, str) and prefix.strip()
    }
    prefixes.add("/")
    ordered = sorted(prefixes, key=len, reverse=True)
    # 允许连续多个前缀（例如 "/!"），最多剥离 8 次避免异常配置导致死循环
    for _ in range(8):
        changed = False
        for prefix in ordered:
            if text.startswith(prefix):
                text = text[len(prefix) :].lstrip()
                changed = True
                break
        if not changed:
            break
    return _collapse(text).casefold()


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _command_label(text: str, wake_prefixes: Sequence[str]) -> str:
    """取一条消息的首个 token 作为日志里的指令名。"""
    normalized = normalize_command_entry(text, wake_prefixes)
    if not normalized:
        return ""
    first = normalized.split(" ", 1)[0]
    if len(first) <= _COMMAND_LABEL_LIMIT:
        return first
    return first[:_COMMAND_LABEL_LIMIT] + "…"


def match_text_prefix(
    entries: Sequence[str],
    message_text: str,
    wake_prefixes: Sequence[str] = DEFAULT_WAKE_PREFIXES,
) -> str | None:
    """判断消息文本是否命中配置项，命中时返回该配置项。

    只按**完整词边界**匹配：文本与配置项完全相等，或以「配置项 + 一个空格」开头。
    因此配置 ``cr`` 会命中 ``cr``、``cr test``，但不会命中 ``crtest``、``crx``、
    ``cr-test``、``cr,test``。
    """
    normalized = normalize_command_entry(message_text, wake_prefixes)
    if not normalized:
        return None
    for entry in entries:
        if not entry:
            continue
        if normalized == entry or normalized.startswith(entry + " "):
            return entry
    return None


@dataclass(frozen=True)
class Decision:
    """一次忽略判定的结果。"""

    ignored: bool
    reason: str
    command: str = ""
    entry: str = ""
    wake_kind: str = ""

    @property
    def reason_text(self) -> str:
        """给日志用的中文原因描述。"""
        return {
            REASON_NOT_WOKEN: "非唤醒消息",
            REASON_ADMIN: "管理员放行",
            REASON_BOT_SELF: "机器人自身事件",
            REASON_EXEMPT: "命中放行名单",
            REASON_BLOCKED: "命中屏蔽指令",
            REASON_NON_ADMIN_WAKE: "非管理员唤醒词",
            REASON_ALLOWED: "未被忽略",
        }.get(self.reason, self.reason)


@dataclass(frozen=True)
class GuardPolicy:
    """插件配置解析后的策略快照。"""

    ignore_non_admin: bool = False
    ignored_commands: tuple[str, ...] = ()
    exempt_commands: tuple[str, ...] = ()
    hard_silence: bool = True
    wake_prefixes: tuple[str, ...] = DEFAULT_WAKE_PREFIXES

    # ---------------------------------------------------------------- 构建

    @classmethod
    def from_config(
        cls,
        raw: Any,
        wake_prefixes: Sequence[str] | None = None,
    ) -> "GuardPolicy":
        """从插件配置（dict / AstrBotConfig）构建策略快照。"""
        config = raw if isinstance(raw, dict) else {}
        prefixes = tuple(
            prefix.strip()
            for prefix in (wake_prefixes or DEFAULT_WAKE_PREFIXES)
            if isinstance(prefix, str) and prefix.strip()
        ) or DEFAULT_WAKE_PREFIXES

        def _entries(key: str) -> tuple[str, ...]:
            return _dedupe(
                normalize_command_entry(item, prefixes)
                for item in _as_str_list(config.get(key))
            )

        return cls(
            ignore_non_admin=_as_bool(config.get("ignore_non_admin"), False),
            ignored_commands=_entries("ignored_commands"),
            exempt_commands=_entries("exempt_commands"),
            hard_silence=_as_bool(config.get("hard_silence"), True),
            wake_prefixes=prefixes,
        )

    # ---------------------------------------------------------------- 判定

    def resolve(
        self,
        *,
        message_text: str,
        wake_kind: str = WAKE_OTHER,
        sender_id: str = "",
        self_id: str = "",
        is_admin: bool = False,
    ) -> Decision:
        """对本次已唤醒消息做最终判定。

        优先级（从高到低）：管理员 → 机器人自身 → 放行名单 → 屏蔽指令 →
        忽略非管理员唤醒词。
        """
        text = str(message_text or "")
        command = _command_label(text, self.wake_prefixes)

        def _decision(ignored: bool, reason: str, entry: str = "") -> Decision:
            return Decision(
                ignored=ignored,
                reason=reason,
                command=entry or command,
                entry=entry,
                wake_kind=wake_kind,
            )

        # 管理员始终放行：直接复用 AstrBot 自带的管理员身份，无需额外配置。
        if is_admin:
            return _decision(False, REASON_ADMIN)

        # 机器人自身发出的事件始终放行，交由 AstrBot 自行处理。
        if sender_id and self_id and str(sender_id) == str(self_id):
            return _decision(False, REASON_BOT_SELF)

        exempt_hit = match_text_prefix(
            self.exempt_commands,
            text,
            self.wake_prefixes,
        )
        if exempt_hit is not None:
            return _decision(False, REASON_EXEMPT, exempt_hit)

        blocked_hit = match_text_prefix(
            self.ignored_commands,
            text,
            self.wake_prefixes,
        )
        if blocked_hit is not None:
            return _decision(True, REASON_BLOCKED, blocked_hit)

        if wake_kind == WAKE_PREFIX and self.ignore_non_admin:
            return _decision(True, REASON_NON_ADMIN_WAKE)

        return _decision(False, REASON_ALLOWED)


__all__ = [
    "Decision",
    "DEFAULT_WAKE_PREFIXES",
    "GuardPolicy",
    "REASON_ADMIN",
    "REASON_ALLOWED",
    "REASON_BLOCKED",
    "REASON_BOT_SELF",
    "REASON_EXEMPT",
    "REASON_NON_ADMIN_WAKE",
    "REASON_NOT_WOKEN",
    "WAKE_DIRECT",
    "WAKE_OTHER",
    "WAKE_PREFIX",
    "WAKE_PRIVATE",
    "match_text_prefix",
    "normalize_command_entry",
]
