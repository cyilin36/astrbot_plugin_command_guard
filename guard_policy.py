"""指令守卫的策略解析与判定逻辑。

本模块刻意不导入 astrbot：所有配置归一化、会话规则匹配与忽略判定都是纯函数，
便于在没有 AstrBot 运行时的环境下直接做单元测试。

术语：
- 全局设置：插件配置里的 ``ignore_non_admin`` / ``ignored_commands`` /
  ``exempt_commands`` / ``hard_silence``；
- 会话规则：``session_rules`` 列表中的一项，按顺序匹配 UMO，首个命中生效；
  规则只在命中的会话内生效，不会影响其它会话；
- 列表模式：``append`` 追加到全局之后、``replace`` 覆盖全局（没有 ``inherit``）。

固定行为（不可配置）：管理员始终放行；机器人自身发出的事件始终放行；
被忽略的指令始终记录一条日志。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

LIST_APPEND = "append"
LIST_REPLACE = "replace"
LIST_VALUES = (LIST_APPEND, LIST_REPLACE)

DEFAULT_WAKE_PREFIXES: tuple[str, ...] = ("/",)

_TRUE_TOKENS = frozenset({"on", "true", "1", "yes", "y", "enable", "enabled"})
_FALSE_TOKENS = frozenset({"off", "false", "0", "no", "n", "disable", "disabled"})
_INHERIT_TOKENS = frozenset({"", "inherit", "global", "default"})

_WHITESPACE_RE = re.compile(r"\s+")

# 判定原因（写日志时使用英文 token，便于 grep）
REASON_SESSION_OFF = "session_off"
REASON_NOT_WOKEN = "not_woken"
REASON_NOT_COMMAND = "not_command"
REASON_ADMIN = "admin"
REASON_BOT_SELF = "bot_self"
REASON_EXEMPT = "exempt"
REASON_NON_ADMIN = "non_admin"
REASON_LISTED = "listed"
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


def _as_optional_bool(value: Any) -> bool | None:
    """解析会话级布尔：``None`` 表示「沿用全局」。

    旧版本配置里的 ``inherit`` / 空值会被识别为 ``None``；新版本 WebUI 只会写
    布尔值，因此新建的会话规则一定是「覆盖全局」。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().lower()
    if token in _INHERIT_TOKENS:
        return None
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return None


def _as_mode(value: Any, default: str = LIST_APPEND) -> str:
    """解析列表合并模式。"""
    token = str(value or "").strip().lower()
    return token if token in LIST_VALUES else default


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
    """归一化用户填写的指令名，用于比较。

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


def normalize_command_name(raw: Any) -> str:
    """归一化从 handler 读到的指令名（不剥离唤醒前缀，仅折叠空白与大小写）。"""
    return _collapse(str(raw or "")).casefold()


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _merge_lists(
    global_items: Sequence[str],
    rule_items: Sequence[str],
    mode: str,
) -> tuple[str, ...]:
    """按 append/replace 合并全局列表与会话列表。

    ``append`` 且会话列表为空时，结果就是全局列表（即「只用全局」）。
    """
    if mode == LIST_REPLACE:
        return _dedupe(rule_items)
    return _dedupe([*global_items, *rule_items])


@dataclass(frozen=True)
class SessionRule:
    """一条会话级规则，只对命中的 UMO 生效。"""

    pattern: str
    index: int
    enabled: bool = True
    ignore_non_admin: bool | None = None
    """``None`` 表示沿用全局值；布尔值表示覆盖全局（仅本会话）。"""

    ignored_mode: str = LIST_APPEND
    ignored_commands: tuple[str, ...] = ()
    exempt_mode: str = LIST_APPEND
    exempt_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """一次忽略判定的结果。"""

    ignored: bool
    reason: str
    command: str = ""
    scope: str = "global"
    rule_index: int | None = None
    matched: tuple[str, ...] = ()
    entry: str = ""

    @property
    def reason_text(self) -> str:
        """给日志用的中文原因描述。"""
        return {
            REASON_SESSION_OFF: "该会话已关闭守卫",
            REASON_NOT_WOKEN: "非唤醒消息",
            REASON_NOT_COMMAND: "不是指令",
            REASON_ADMIN: "管理员放行",
            REASON_BOT_SELF: "机器人自身事件",
            REASON_EXEMPT: "命中放行名单",
            REASON_NON_ADMIN: "非管理员指令",
            REASON_LISTED: "指令在忽略列表",
            REASON_ALLOWED: "未被忽略",
        }.get(self.reason, self.reason)


@dataclass(frozen=True)
class GuardPolicy:
    """插件配置解析后的策略快照。"""

    ignore_non_admin: bool = False
    ignored_commands: tuple[str, ...] = ()
    exempt_commands: tuple[str, ...] = ()
    hard_silence: bool = True
    rules: tuple[SessionRule, ...] = ()
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

        rules: list[SessionRule] = []
        raw_rules = config.get("session_rules")
        if isinstance(raw_rules, list):
            for item in raw_rules:
                if not isinstance(item, dict):
                    continue
                # umo_custom 优先，便于为尚未出现在会话列表里的会话手填规则
                pattern = str(
                    item.get("umo_custom") or item.get("umo") or ""
                ).strip()
                if not pattern:
                    continue
                rules.append(
                    SessionRule(
                        pattern=pattern,
                        index=len(rules),
                        enabled=_as_bool(item.get("enabled"), True),
                        ignore_non_admin=_as_optional_bool(
                            item.get("ignore_non_admin")
                        ),
                        ignored_mode=_as_mode(item.get("ignored_mode")),
                        ignored_commands=_dedupe(
                            normalize_command_entry(entry, prefixes)
                            for entry in _as_str_list(item.get("ignored_commands"))
                        ),
                        exempt_mode=_as_mode(item.get("exempt_mode")),
                        exempt_commands=_dedupe(
                            normalize_command_entry(entry, prefixes)
                            for entry in _as_str_list(item.get("exempt_commands"))
                        ),
                    )
                )

        return cls(
            ignore_non_admin=_as_bool(config.get("ignore_non_admin"), False),
            ignored_commands=_entries("ignored_commands"),
            exempt_commands=_entries("exempt_commands"),
            hard_silence=_as_bool(config.get("hard_silence"), True),
            rules=tuple(rules),
            wake_prefixes=prefixes,
        )

    # ---------------------------------------------------------------- 规则

    @staticmethod
    def rule_matches(umo: str, pattern: str) -> bool:
        """判断 UMO 是否命中规则模式。

        - 含 ``*`` / ``?`` / ``[``：按通配符匹配（``fnmatch``）；
        - 含 ``:``：按完整 UMO 精确匹配；
        - 纯数字（或其他不含 ``:`` 的片段）：匹配 UMO 最后一段（群号 / 用户号）。
        """
        umo_text = str(umo or "").strip()
        pattern_text = str(pattern or "").strip()
        if not umo_text or not pattern_text:
            return False
        if any(char in pattern_text for char in "*?["):
            return fnmatch.fnmatchcase(umo_text, pattern_text)
        if ":" in pattern_text:
            return umo_text == pattern_text
        return umo_text.rsplit(":", 1)[-1] == pattern_text

    def match_rule(self, umo: str) -> SessionRule | None:
        """返回首个命中该 UMO 的会话规则。"""
        for rule in self.rules:
            if self.rule_matches(umo, rule.pattern):
                return rule
        return None

    def is_enabled_for(self, umo: str) -> bool:
        """判断某个会话最终是否启用守卫。"""
        rule = self.match_rule(umo)
        if rule is None:
            return True
        return rule.enabled

    def ignore_non_admin_for(self, umo: str) -> bool:
        """判断某个会话最终是否忽略非管理员指令。

        会话规则里显式设置了该开关时覆盖全局值（仅本会话生效）；
        未设置（旧配置里的 ``inherit``）时沿用全局值。
        """
        rule = self.match_rule(umo)
        if rule is None or rule.ignore_non_admin is None:
            return self.ignore_non_admin
        return rule.ignore_non_admin

    def effective_ignored(self, rule: SessionRule | None) -> tuple[str, ...]:
        """某个会话最终生效的忽略列表。"""
        if rule is None:
            return self.ignored_commands
        return _merge_lists(
            self.ignored_commands,
            rule.ignored_commands,
            rule.ignored_mode,
        )

    def effective_exempt(self, rule: SessionRule | None) -> tuple[str, ...]:
        """某个会话最终生效的放行列表。"""
        if rule is None:
            return self.exempt_commands
        return _merge_lists(
            self.exempt_commands,
            rule.exempt_commands,
            rule.exempt_mode,
        )

    # ---------------------------------------------------------------- 判定

    def resolve(
        self,
        *,
        umo: str,
        matched_commands: Sequence[str],
        sender_id: str = "",
        self_id: str = "",
        is_admin: bool = False,
    ) -> Decision:
        """对“本次消息命中的指令”做最终判定。

        优先级（从高到低）：会话守卫关闭 → 管理员 → 机器人自身 → 放行名单 →
        忽略非管理员 → 忽略列表。
        """
        matched = tuple(name for name in matched_commands if name)
        command = matched[0] if matched else ""
        rule = self.match_rule(umo)
        scope = "session" if rule is not None else "global"
        rule_index = rule.index if rule is not None else None

        def _decision(ignored: bool, reason: str, entry: str = "") -> Decision:
            return Decision(
                ignored=ignored,
                reason=reason,
                command=command,
                scope=scope,
                rule_index=rule_index,
                matched=matched,
                entry=entry,
            )

        if not self.is_enabled_for(umo):
            return _decision(False, REASON_SESSION_OFF)

        # 管理员始终放行：直接复用 AstrBot 自带的管理员身份，无需额外配置。
        if is_admin:
            return _decision(False, REASON_ADMIN)

        # 机器人自身发出的事件始终放行，交由 AstrBot 自行处理。
        if sender_id and self_id and str(sender_id) == str(self_id):
            return _decision(False, REASON_BOT_SELF)

        exempt_hit = match_command_entries(self.effective_exempt(rule), matched)
        if exempt_hit is not None:
            return _decision(False, REASON_EXEMPT, exempt_hit[0])

        if self.ignore_non_admin_for(umo):
            return _decision(True, REASON_NON_ADMIN)

        ignored_hit = match_command_entries(self.effective_ignored(rule), matched)
        if ignored_hit is not None:
            return _decision(True, REASON_LISTED, ignored_hit[0])

        return _decision(False, REASON_ALLOWED)


def match_command_entries(
    entries: Sequence[str],
    matched_commands: Sequence[str],
) -> tuple[str, str] | None:
    """判断配置项是否命中本次消息的指令名。

    返回 ``(命中的配置项, 命中的指令名)``，未命中返回 ``None``。
    匹配规则：完全相等，或以 ``配置项 + 空格`` 开头（因此配置指令组名即可命中其子指令）。
    """
    if not entries or not matched_commands:
        return None
    normalized = [
        (normalize_command_name(name), name) for name in matched_commands if name
    ]
    for entry in entries:
        for normalized_name, display_name in normalized:
            if not normalized_name:
                continue
            if normalized_name == entry or normalized_name.startswith(entry + " "):
                return entry, display_name
    return None
