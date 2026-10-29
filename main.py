"""AstrBot 指令守卫插件。

按全局设置与 UMO 规则，屏蔽非管理员的唤醒词消息与指定指令：
被屏蔽的消息不会被执行、不会产生任何回复，也不会触发 LLM 请求；每次屏蔽都会在
AstrBot 日志中留下一条 INFO 记录（固定行为，不可关闭）。

规则来源：

- 全局设置：``ignore_non_admin`` / ``ignored_commands`` / ``exempt_commands`` /
  ``hard_silence``；
- UMO 规则：``umo_rules`` 中按 ``event.unified_msg_origin`` 精确匹配的条目。
  命中条目后**完全覆盖**全局设置，不做继承、追加或合并；没有命中的会话才使用
  全局设置。

屏蔽方式：

- 「忽略非管理员唤醒词」只拦截通过 ``wake_prefix`` 唤醒的消息；直接 @bot、
  回复 bot、私聊不受影响；
- 「屏蔽指令」按消息文本前缀匹配，唤醒词与直接 @bot 两条路径都生效。

固定行为（无需配置）：

- 管理员始终放行，直接复用 AstrBot 自带的 ``admins_id`` 身份；
- 机器人自身发出的事件始终放行，交给 AstrBot 自行处理；
- 每次屏蔽都记录日志。

实现分为两层（详见 README）：

1. 静默过滤器注入：把守卫过滤器插到其它插件指令 handler 的 ``event_filters`` 最前面，
   使其在 AstrBot 的唤醒检查阶段就被跳过，连框架自带的「权限不足 / 参数不足」提示
   也不会发出；需要静默时直接 ``stop_event()``，让管道在唤醒阶段结束后就跳出，
   后续阶段与 LLM 请求都不会执行。
2. 高优先级兜底监听器：即使某个 handler 没有被注入，也能在消息事件阶段直接
   ``stop_event()``，避免指令执行与 LLM 请求。
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from .guard_commands import (
    classify_wake_kind,
    handler_command_names,
    is_handler_enabled,
    message_text_for_matching,
)
from .guard_policy import (
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

# 影响判定的配置键；其余键（例如 WebUI 的临时状态）变化时不重建策略。
POLICY_KEYS = (
    "ignore_non_admin",
    "ignored_commands",
    "exempt_commands",
    "hard_silence",
    "umo_rules",
)

# UMO 下拉候选：优先读取 AstrBot 会话表；回退到会话管理器时按页读取。
UMO_PAGE_SIZE = 500
UMO_MAX_PAGES = 10


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

    判定为「应忽略」时返回 ``False``，让该 handler 在唤醒检查阶段就被跳过；
    若该事件按 UMO/全局设置需要静默，还会直接 ``stop_event()``，让事件停在
    ``WakingCheckStage``，不再进入后续的限流、内容安全、预处理与 LLM 请求等环节。

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
        if decision is None or not decision.ignored:
            return True
        if not decision.hard_silence:
            # 该 UMO 关闭了静默处理：只跳过 handler，不提前终止事件，
            # 后续由兜底监听器在处理阶段拦截。
            return False
        # 在唤醒检查阶段就终止事件传播：管道调度器发现 is_stopped() 后会直接
        # 跳出，后续阶段（含 LLM 请求）都不会执行。
        try:
            event.stop_event()
        except Exception as exc:  # pragma: no cover - 极端情况下退回兜底层
            logger.debug(f"{LOG_PREFIX} 提前终止事件失败，交由兜底监听器处理: {exc}")
        return False


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
    """指令守卫：全局设置 + UMO 规则，忽略指定指令与非管理员指令。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._policy: GuardPolicy | None = None
        self._policy_fingerprint: str | None = None
        self._command_handlers: list = []
        self._installed_hard_silence: bool | None = None
        self._reinstall_task: asyncio.Task | None = None
        self._known_umos: set[str] = set()
        """运行时见过的 UMO，用于把规则里的 UMO 字段渲染成下拉选项。"""
        self._umos_loaded = False
        """是否已从 AstrBot 会话表读取过历史 UMO（失败时下次重试）。"""

        GuardInjectFilter.plugin = self
        GuardTriggerFilter.plugin = self

        policy = self.get_policy()
        logger.info(
            f"{LOG_PREFIX} {PLUGIN_NAME} 已加载 | "
            f"忽略非管理员唤醒词={'开' if policy.ignore_non_admin else '关'} "
            f"屏蔽指令={len(policy.ignored_commands)} 放行指令={len(policy.exempt_commands)} "
            f"UMO 规则={len(policy.rules)} 静默处理={'开' if policy.hard_silence else '关'}"
        )

    # ------------------------------------------------------------------ 配置

    def _config_fingerprint(self) -> str:
        """配置指纹，用于在 WebUI 改配置后自动重建策略。"""
        snapshot = {
            key: self.config.get(key) if isinstance(self.config, dict) else None
            for key in POLICY_KEYS
        }
        try:
            return json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            return repr(snapshot)

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
            # 让 WebUI 新建 UMO 条目时能复制当前全局设置，并补齐下拉选项。
            self._refresh_umo_schema(self._policy)
            self._schedule_reinstall_if_needed()
        return self._policy

    def _schedule_reinstall_if_needed(self) -> None:
        """「静默处理」需求被改动时，安排一次重新注入。

        不在判定过程中同步改 ``event_filters``：当前消息可能正在被框架遍历，
        改成异步任务可确保在下一个事件循环周期里安全生效。
        """
        policy = self._policy
        if policy is None:
            return
        if self._installed_hard_silence is None:
            return
        if policy.injection_needed() == self._installed_hard_silence:
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

    # -------------------------------------------------------------- UMO 选项

    def _configured_umos(self) -> set[str]:
        """已保存在 UMO 规则里的会话，保证旧配置仍显示在下拉选项里。"""
        umos: set[str] = set()
        raw_rules = (
            self.config.get("umo_rules") if isinstance(self.config, dict) else None
        )
        if not isinstance(raw_rules, list):
            return umos
        for item in raw_rules:
            if not isinstance(item, dict):
                continue
            value = item.get("umo")
            if isinstance(value, str) and value.strip():
                umos.add(value.strip())
        return umos

    def _refresh_umo_schema(self, policy: GuardPolicy | None = None) -> None:
        """把全局默认值和 UMO 下拉选项写进内存中的配置 schema。

        AstrBot 的 WebUI 每次从 ``AstrBotConfig.schema`` 读取配置结构：
        - 修改模板 item 的 ``default``，新建条目时就会自动复制当前全局设置；
        - 修改 ``umo`` 的 ``options``，就把自由输入变成下拉选择。

        只改内存，不做 IO；任何异常都静默降级，最多让下拉少几项，不影响拦截。
        """
        try:
            policy = policy if policy is not None else self._policy
            if policy is None:
                return
            schema = getattr(self.config, "schema", None)
            if not isinstance(schema, dict):
                return
            rules_meta = schema.get("umo_rules")
            if not isinstance(rules_meta, dict):
                return
            templates = rules_meta.get("templates")
            if not isinstance(templates, dict):
                return
            template = templates.get("umo_rule")
            if not isinstance(template, dict):
                return
            items = template.get("items")
            if not isinstance(items, dict):
                return

            defaults = {
                "ignore_non_admin": policy.ignore_non_admin,
                "ignored_commands": list(policy.ignored_commands),
                "exempt_commands": list(policy.exempt_commands),
                "hard_silence": policy.hard_silence,
            }
            for key, value in defaults.items():
                item_meta = items.get(key)
                if isinstance(item_meta, dict):
                    item_meta["default"] = copy.deepcopy(value)

            umo_meta = items.get("umo")
            if isinstance(umo_meta, dict):
                options = sorted(self._known_umos | self._configured_umos())
                if options:
                    umo_meta["options"] = options
                else:
                    # 空列表会被前端当成「存在 options」并渲染成空白下拉，必须删掉。
                    umo_meta.pop("options", None)
        except Exception as exc:  # 纯展示功能，失败不影响判定
            logger.debug(f"{LOG_PREFIX} 刷新 UMO 配置结构失败: {exc}")

    def _note_umo(self, umo: str) -> None:
        """记录运行时见到的 UMO，新会话也能出现在下拉框里。"""
        text = str(umo or "").strip()
        if not text or text in self._known_umos:
            return
        self._known_umos.add(text)
        self._refresh_umo_schema()

    async def _load_umos_from_sql(self) -> bool:
        """用 AstrBot Dashboard 同款的去重查询读取 UMO。"""
        context = self.context
        if context is None:
            return False
        get_db = getattr(context, "get_db", None)
        if not callable(get_db):
            return False
        db = get_db()
        if db is None:
            return False

        # 懒加载内部 API：测试环境的 astrbot stub 没有数据库模块，失败就走回退。
        from sqlmodel import select

        from astrbot.core.db.po import ConversationV2

        async with db.get_db() as session:
            result = await session.execute(select(ConversationV2.user_id).distinct())
            for row in result.fetchall():
                value = str(row[0] or "").strip()
                if value:
                    self._known_umos.add(value)

        # UMO 别名可能对应没有对话记录的历史会话，顺手合并。
        try:
            aliases = await db.get_umo_aliases()
            for alias in aliases:
                value = str(getattr(alias, "umo", "") or "").strip()
                if value:
                    self._known_umos.add(value)
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} 读取 UMO 别名失败: {exc}")
        return True

    async def _load_umos_from_conversations(self) -> bool:
        """回退方案：通过会话管理器分页读取历史会话。"""
        context = self.context
        if context is None:
            return False
        manager = getattr(context, "conversation_manager", None)
        getter = getattr(manager, "get_filtered_conversations", None)
        if not callable(getter):
            return False

        seen = 0
        for page in range(1, UMO_MAX_PAGES + 1):
            try:
                conversations, total = await getter(
                    page=page,
                    page_size=UMO_PAGE_SIZE,
                    include_history=False,
                )
            except TypeError:
                # 旧版本会话管理器没有 include_history 参数。
                conversations, total = await getter(
                    page=page,
                    page_size=UMO_PAGE_SIZE,
                )
            if not conversations:
                break
            for conversation in conversations:
                value = str(getattr(conversation, "user_id", "") or "").strip()
                if value:
                    self._known_umos.add(value)
            seen += len(conversations)
            try:
                total_count = int(total or 0)
            except (TypeError, ValueError):
                total_count = 0
            if total_count and seen >= total_count:
                break
        return True

    async def _load_umos_from_db(self) -> None:
        """读取 AstrBot 已有 UMO，供规则下拉选择；失败不影响守卫。"""
        if self._umos_loaded:
            return
        loaded = False
        try:
            loaded = await self._load_umos_from_sql()
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} 读取会话表失败，尝试回退: {exc}")
        if not loaded:
            try:
                loaded = await self._load_umos_from_conversations()
            except Exception as exc:
                logger.debug(f"{LOG_PREFIX} 读取会话列表失败，UMO 下拉可能不全: {exc}")
        if loaded:
            self._umos_loaded = True
        self._refresh_umo_schema()

    # -------------------------------------------------------------- 判定入口

    def _is_woken_message(self, event: AstrMessageEvent) -> bool:
        """只有被 AstrBot 唤醒的消息才进入守卫判定。"""
        return bool(getattr(event, "is_at_or_wake_command", False))

    def _candidate_handlers(self) -> list:
        """当前注册表中所有指令 handler（排除本插件自身与已禁用项）。"""
        own_module = __name__
        handlers: list = []
        for handler in star_handlers_registry:
            if getattr(handler, "event_type", None) != EventType.AdapterMessageEvent:
                continue
            if getattr(handler, "handler_module_path", None) == own_module:
                continue
            if not is_handler_enabled(handler):
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

        umo = ""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        except Exception:
            umo = ""
        self._note_umo(umo)

        if not self._is_woken_message(event):
            decision = Decision(ignored=False, reason=REASON_NOT_WOKEN, umo=umo)
            event.set_extra(DECISION_EXTRA_KEY, decision)
            return decision

        sender_id = ""
        self_id = ""
        try:
            sender_id = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} 读取发送者信息失败: {exc}")

        is_admin = False
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False

        decision = policy.resolve(
            message_text=message_text_for_matching(event),
            wake_kind=classify_wake_kind(event, policy.wake_prefixes),
            sender_id=sender_id,
            self_id=self_id,
            is_admin=is_admin,
            umo=umo,
        )
        event.set_extra(DECISION_EXTRA_KEY, decision)
        return decision

    def log_decision(self, event: AstrMessageEvent, decision: Decision) -> None:
        """被忽略时记录一条日志（每个事件最多一条，固定记录，不可关闭）。"""
        if not decision.ignored:
            return
        if event.get_extra(LOGGED_EXTRA_KEY, False):
            return
        event.set_extra(LOGGED_EXTRA_KEY, True)

        try:
            sender = f"{event.get_sender_name()}/{event.get_sender_id()}"
        except Exception:
            sender = "unknown"
        scope = decision.scope or "global"
        rule = (
            f"#{decision.rule_index + 1}"
            if decision.rule_index is not None
            else "-"
        )
        logger.info(
            f"{LOG_PREFIX} 已忽略 | umo={decision.umo or '-'} sender={sender} "
            f"wake={decision.wake_kind or '-'} cmd={decision.command or '-'} "
            f"reason={decision.reason}({decision.reason_text}) "
            f"entry={decision.entry or '-'} scope={scope} rule={rule}"
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
        needed = policy.injection_needed()

        injected = 0
        for handler in self._command_handlers:
            try:
                self._remove_guard_filters(handler)
                if not needed:
                    continue
                handler.event_filters.insert(0, GuardInjectFilter(raise_error=False))
                injected += 1
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 注入过滤器失败（{getattr(handler, 'handler_full_name', '?')}）：{exc}"
                )

        self._installed_hard_silence = needed
        logger.info(
            f"{LOG_PREFIX} 静默过滤器已就绪（{reason}）| 指令 handler={len(self._command_handlers)} "
            f"已注入={injected} 静默处理={'开' if needed else '关'}"
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

    async def initialize(self):
        """AstrBot 实例化插件后调用：补齐 UMO 下拉选项。"""
        await self._load_umos_from_db()
        if self._known_umos:
            logger.info(f"{LOG_PREFIX} UMO 候选={len(self._known_umos)}")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后统一注入，覆盖先前加载的所有插件。"""
        await self._load_umos_from_db()
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
