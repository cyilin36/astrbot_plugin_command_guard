"""AstrBot 指令守卫插件。

按全局默认设置 + 会话（UMO）精细化规则，忽略指定的指令，以及非管理员发出的指令。
被忽略的指令不会被执行、不会产生任何回复，也不会触发 LLM 请求；仅在 AstrBot 日志中
留下一条记录（可通过 ``log_ignored`` 关闭）。

实现分为两层（详见 README）：

1. 静默过滤器注入：把守卫过滤器插到其它插件指令 handler 的 ``event_filters`` 最前面，
   使其在 AstrBot 的唤醒检查阶段就被跳过，连框架自带的「权限不足 / 参数不足」提示
   也不会发出。
2. 高优先级兜底监听器：即使某个 handler 没有被注入，也能在消息事件阶段直接
   ``stop_event()``，避免指令执行与 LLM 请求。
"""

from __future__ import annotations

import asyncio
import json
import sys

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from .guard_commands import handler_command_names, matched_commands_for_event
from .guard_policy import (
    REASON_DISABLED,
    REASON_NOT_COMMAND,
    REASON_NOT_WOKEN,
    Decision,
    GuardPolicy,
)

PLUGIN_NAME = "astrbot_plugin_command_guard"
"""插件名，与 metadata.yaml 中的 name 保持一致。"""
LOG_PREFIX = "[cmd_guard]"

# 内置插件（astrbot）占用了 maxsize 与 maxsize - 1，这里紧随其后。
WATCHDOG_PRIORITY = sys.maxsize - 2

DECISION_EXTRA_KEY = "_cmd_guard_decision"
LOGGED_EXTRA_KEY = "_cmd_guard_logged"


class GuardDecisionMixin:
    """两条拦截路径共用的判定入口。"""

    plugin: "CommandGuardPlugin | None" = None

    def _decide(self, event: AstrMessageEvent) -> Decision | None:
        """返回本次事件的守卫判定结果；插件不可用时返回 ``None``。"""
        plugin = self.plugin
        if plugin is None:
            return None
        return plugin.decide(event)

    def _decide_and_log(self, event: AstrMessageEvent) -> Decision | None:
        """判定并在「应忽略」时记录日志（同一事件只记一次）。"""
        decision = self._decide(event)
        if decision is not None and decision.ignored:
            plugin = self.plugin
            if plugin is not None:
                plugin.log_decision(event, decision)
        return decision


class GuardInjectFilter(GuardDecisionMixin, filter.CustomFilter):
    """注入到其它指令 handler 的静默过滤器。

    判定为「应忽略」时返回 ``False``，让该 handler 在唤醒检查阶段就被跳过。
    任何异常都返回 ``True``，保证守卫自身的问题不会影响正常链路。
    """

    is_command_guard_filter = True
    """标记位：用于识别并清理历史版本/已卸载实例遗留在 handler 上的过滤器。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        try:
            decision = self._decide_and_log(event)
        except Exception as exc:  # 守卫自身异常绝不能影响正常链路
            logger.warning(f"{LOG_PREFIX} 注入过滤器判定异常，已放行: {exc}")
            return True
        if decision is None:
            return True
        return not decision.ignored


class GuardTriggerFilter(GuardDecisionMixin, filter.CustomFilter):
    """兜底监听器的触发器：仅当本次事件应当被忽略时才激活该监听器。"""

    is_command_guard_filter = True
    """标记位：同上。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        try:
            decision = self._decide_and_log(event)
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 兜底触发器判定异常，已放行: {exc}")
            return False
        return bool(decision and decision.ignored)


class CommandGuardPlugin(Star):
    """指令守卫：全局默认 + 会话精细化规则，忽略指定指令与非管理员指令。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._policy: GuardPolicy | None = None
        self._policy_fingerprint: str | None = None
        self._command_handlers: list = []
        self._installed_hard_silence: bool | None = None
        self._reinstall_task: asyncio.Task | None = None

        GuardInjectFilter.plugin = self
        GuardTriggerFilter.plugin = self

        policy = self.get_policy()
        logger.info(
            f"{LOG_PREFIX} {PLUGIN_NAME} 已加载 | 总开关={'开' if policy.enabled else '关'} "
            f"忽略非管理员={'开' if policy.ignore_non_admin else '关'} "
            f"忽略指令={len(policy.ignored_commands)} 放行指令={len(policy.exempt_commands)} "
            f"会话规则={len(policy.rules)} 静默注入={'开' if policy.hard_silence else '关'}"
        )

    # ------------------------------------------------------------------ 配置

    def _config_fingerprint(self) -> str:
        """配置指纹，用于在 WebUI 改配置后自动重建策略。"""
        try:
            return json.dumps(self.config, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            return repr(self.config)

    def _wake_prefixes(self) -> tuple[str, ...]:
        """读取 AstrBot 的唤醒前缀，用于归一化配置里的指令名。"""
        prefixes: list[str] = []
        try:
            astrbot_config = self.context.get_config()
            raw = astrbot_config.get("wake_prefix") if astrbot_config else None
            if isinstance(raw, (list, tuple)):
                prefixes.extend(str(item) for item in raw if str(item).strip())
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} 读取唤醒前缀失败，使用默认值: {exc}")
        return tuple(prefixes) or ("/",)

    def get_policy(self) -> GuardPolicy:
        """返回当前策略快照，配置变化时自动重建。"""
        fingerprint = self._config_fingerprint()
        if self._policy is None or fingerprint != self._policy_fingerprint:
            self._policy = GuardPolicy.from_config(self.config, self._wake_prefixes())
            self._policy_fingerprint = fingerprint
            self._schedule_reinstall_if_needed()
        return self._policy

    def _schedule_reinstall_if_needed(self) -> None:
        """「彻底静默」开关被改动时，安排一次重新注入。

        不在判定过程中同步改 ``event_filters``：当前消息可能正在被框架遍历，
        改成异步任务可确保在下一个事件循环周期里安全生效。
        """
        policy = self._policy
        if policy is None:
            return
        if self._installed_hard_silence is None:
            return
        if policy.hard_silence == self._installed_hard_silence:
            return
        if self._reinstall_task is not None and not self._reinstall_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reinstall_task = loop.create_task(self._reinstall("config_changed"))

    async def _reinstall(self, reason: str) -> None:
        """异步重新注入。"""
        try:
            self.install(reason)
        except Exception as exc:  # pragma: no cover - 兜底
            logger.warning(f"{LOG_PREFIX} 重新注入过滤器失败: {exc}")

    # -------------------------------------------------------------- 判定入口

    def _is_woken_command(self, event: AstrMessageEvent) -> bool:
        """只有「被唤醒且以指令形式触发」的消息才进入守卫判定。"""
        return bool(getattr(event, "is_at_or_wake_command", False))

    def _candidate_handlers(self) -> list:
        """当前注册表中所有指令 handler（排除本插件自身）。"""
        own_module = __name__
        handlers: list = []
        for handler in star_handlers_registry:
            if getattr(handler, "event_type", None) != EventType.AdapterMessageEvent:
                continue
            if getattr(handler, "handler_module_path", None) == own_module:
                continue
            if not handler_command_names(handler):
                continue
            handlers.append(handler)
        return handlers

    def decide(self, event: AstrMessageEvent) -> Decision:
        """计算（并缓存）本次事件的守卫判定。"""
        cached = event.get_extra(DECISION_EXTRA_KEY)
        if isinstance(cached, Decision):
            return cached

        policy = self.get_policy()
        umo = str(getattr(event, "unified_msg_origin", "") or "")

        if not self._is_woken_command(event):
            decision = Decision(ignored=False, reason=REASON_NOT_WOKEN)
            event.set_extra(DECISION_EXTRA_KEY, decision)
            return decision

        if not policy.is_enabled_for(umo):
            decision = Decision(ignored=False, reason=REASON_DISABLED)
            event.set_extra(DECISION_EXTRA_KEY, decision)
            return decision

        if not self._command_handlers:
            self._command_handlers = self._candidate_handlers()
        matched = matched_commands_for_event(event, self._command_handlers)
        if not matched:
            decision = Decision(ignored=False, reason=REASON_NOT_COMMAND)
            event.set_extra(DECISION_EXTRA_KEY, decision)
            return decision

        sender_id = ""
        self_id = ""
        platform_name = ""
        try:
            sender_id = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            platform_name = str(event.get_platform_name() or "")
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} 读取发送者信息失败: {exc}")

        is_admin = False
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False

        decision = policy.resolve(
            umo=umo,
            matched_commands=matched,
            sender_id=sender_id,
            self_id=self_id,
            platform_name=platform_name,
            is_admin=is_admin,
        )
        event.set_extra(DECISION_EXTRA_KEY, decision)
        return decision

    def log_decision(self, event: AstrMessageEvent, decision: Decision) -> None:
        """被忽略时记录一条日志（每个事件最多一条）。"""
        policy = self.get_policy()
        if not decision.ignored or not policy.log_ignored:
            return
        if event.get_extra(LOGGED_EXTRA_KEY, False):
            return
        event.set_extra(LOGGED_EXTRA_KEY, True)

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            sender = f"{event.get_sender_name()}/{event.get_sender_id()}"
        except Exception:
            sender = "unknown"
        scope = "global"
        if decision.scope == "session" and decision.rule_index is not None:
            scope = f"session(rule#{decision.rule_index + 1})"
        logger.info(
            f"{LOG_PREFIX} 已忽略 | umo={umo} sender={sender} "
            f"cmd={decision.command or '-'} reason={decision.reason}({decision.reason_text}) "
            f"scope={scope} matched={list(decision.matched)}"
        )

    # -------------------------------------------------------------- 兜底监听

    # 装饰器自下而上生效：event_message_type 必须放在最下面，才能把 priority
    # 写进 handler 的 extras_configs（AstrBot 的 get_handler_or_create 对已注册
    # 的 handler 会忽略后续传入的 kwargs）。
    @filter.custom_filter(GuardTriggerFilter, False)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=WATCHDOG_PRIORITY)
    async def guard_watchdog(self, event: AstrMessageEvent):
        """兜底拦截：仅当判定为应忽略时被激活，静默终止事件传播。"""
        decision = self.decide(event)
        if not decision.ignored:
            return
        self.log_decision(event, decision)
        event.stop_event()

    # ---------------------------------------------------------------- 注入层

    def _remove_guard_filters(self, handler) -> int:
        """移除某个 handler 上已注入的守卫过滤器，返回移除数量。

        按标记位而不是 ``isinstance`` 识别，这样插件重载后遗留的旧实例过滤器
        也能被正确清理。
        """
        filters = getattr(handler, "event_filters", None)
        if not filters:
            return 0
        kept = [
            event_filter
            for event_filter in filters
            if not getattr(event_filter, "is_command_guard_filter", False)
        ]
        removed = len(filters) - len(kept)
        if removed:
            handler.event_filters = kept
        return removed

    def install(self, reason: str = "startup") -> int:
        """（重新）注入静默过滤器，幂等。返回已注入的 handler 数量。"""
        self._command_handlers = self._candidate_handlers()
        policy = self.get_policy()

        injected = 0
        for handler in self._command_handlers:
            try:
                self._remove_guard_filters(handler)
                if not policy.hard_silence:
                    continue
                handler.event_filters.insert(0, GuardInjectFilter(raise_error=False))
                injected += 1
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 注入过滤器失败（{getattr(handler, 'handler_full_name', '?')}）：{exc}"
                )

        self._installed_hard_silence = policy.hard_silence
        logger.info(
            f"{LOG_PREFIX} 静默过滤器已就绪（{reason}）| 指令 handler={len(self._command_handlers)} "
            f"已注入={injected} 静默={'开' if policy.hard_silence else '关'}"
        )
        return injected

    def uninstall(self) -> None:
        """移除全部注入的过滤器（插件卸载/停用时调用）。

        注意：跳过本插件自身 handler，避免把兜底监听器自己的触发器一并删掉。
        """
        for handler in list(star_handlers_registry):
            if getattr(handler, "handler_module_path", None) == __name__:
                continue
            try:
                self._remove_guard_filters(handler)
            except Exception:
                continue
        self._installed_hard_silence = None

    # -------------------------------------------------------------- 生命周期

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后统一注入，覆盖先前加载的所有插件。"""
        self.install("astrbot_loaded")

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata=None):
        """有新插件加载完成时重新注入，覆盖后加载的插件。"""
        self.install("plugin_loaded")

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self, metadata=None):
        """插件卸载后重新扫描注册表，清理已失效的注入并重新覆盖存活 handler。"""
        self.install("plugin_unloaded")

    async def terminate(self):
        """插件停用时清理注入与类级引用。"""
        task = self._reinstall_task
        self._reinstall_task = None
        if task is not None and not task.done():
            task.cancel()
        self.uninstall()
        GuardInjectFilter.plugin = None
        GuardTriggerFilter.plugin = None
        logger.info(f"{LOG_PREFIX} {PLUGIN_NAME} 已卸载，注入的过滤器已移除。")


__all__ = [
    "CommandGuardPlugin",
    "GuardInjectFilter",
    "GuardTriggerFilter",
]
