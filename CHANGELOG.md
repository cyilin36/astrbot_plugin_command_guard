# 更新日志

## 3.0.0-beta2

按 UMO 细化的规则回归，语义与 2.x 不同。

**行为**

- 新增 `umo_rules` 配置：按 `event.unified_msg_origin` 精确匹配会话。
- 命中条目后**完全覆盖**全局设置，只使用条目里的 `ignore_non_admin` / `ignored_commands` / `exempt_commands` / `hard_silence`；没有继承、追加、合并或 `append`/`replace` 模式。
- 条目可用 `enabled=false` 整体关闭该会话的守卫。
- 管理员、机器人自身事件仍固定放行；`exempt_commands` 在条目内仍然是最高优先级的放行项。
- 静默处理支持按条目覆盖：只要全局或任一启用条目需要静默就注入过滤器，具体事件按自己的设置决定是否在唤醒阶段提前终止。

**配置**

- `umo` 字段为下拉选择，选项来自 AstrBot 已有会话（会话表 + UMO 别名），插件启动时加载、运行时遇到新 UMO 自动补充；列表为空时退回手动输入。
- 新建条目会自动复制当前全局设置（`ignore_non_admin`、`ignored_commands`、`exempt_commands`、`hard_silence`），方便针对该 UMO 微调。
- 旧 2.x 的 `session_rules` 不读取、不迁移；如仍需要，请在新的 `umo_rules` 中重建。

**日志**

- 日志增加 `umo=`、`scope=global|session`、`rule=#N`，便于确认命中的是全局还是某个 UMO 条目。

## 3.0.0-beta1

全局访问控制改版，破坏性变更。

**行为**

- `ignore_non_admin` 的语义从「忽略非管理员的所有指令」改为「忽略非管理员发出的唤醒词消息」；直接 @bot、回复 bot、私聊不再受该开关影响，仍可正常交给 AstrBot。
- `ignored_commands` 改为按消息文本完整词边界匹配，唤醒词与直接 @bot 两条路径都生效；不再要求对应指令已注册。填 `cr` 会屏蔽 `cr test`，但不会屏蔽 `crtest`、`crx`、`cr-test`。
- 新增唤醒方式识别：`direct`（@bot / 回复 bot）、`prefix`（唤醒词）、`private`（私聊）、`other`（@全体等），只有 `prefix` 受唤醒词开关影响。
- 管理员与机器人自身事件始终放行；`exempt_commands` 仍是最高优先级的非管理员放行名单。

**配置**

- 移除全部会话（UMO）规则、UMO 下拉与 `session_rules` 配置项；插件只保留全局配置。
- 保留 `ignore_non_admin`、`ignored_commands`、`exempt_commands`、`hard_silence` 四个键，避免旧配置值丢失。
- `hard_silence` 的 WebUI 文案改为「静默处理」，默认值仍为 `true`。

**日志**

- 日志增加 `wake=` 与 `entry=` 字段，屏蔽原因改为 `blocked_command` / `non_admin_wake`。

## 2.0.1

- 命中即终止：注入过滤器判定为忽略时会同时 `stop_event()`，事件在唤醒检查阶段就被终止，不再进入限流、内容安全、预处理等后续阶段。此前单靠注入只能跳过指令 handler，消息仍会继续走到 LLM。
- 补充指令组说明与测试：填一个组名（如 `cr`）即屏蔽 `cr`、`cr list`、`cr lora` 等整组指令，裸组名的「参数不足 + 指令树」提示同样不会发出。
- README 新增「指令组」与「命中之后」两节。

## 2.0.0

精简配置面与操作方式，破坏性变更（旧配置项会被 AstrBot 自动清理）。

**全局设置**

- 移除总开关 `enabled`：插件常驻运行，默认配置下不拦截任何指令。
- 移除 `admin_bypass`、`extra_admins`：管理员固定放行，直接复用 AstrBot 自带的 `admins_id`。
- 移除 `trust_bot_self`：机器人自身事件固定放行。
- 移除 `log_ignored`：每次忽略必然记录一条 INFO 日志。
- `hard_silence` 保留，默认开启。

**会话（UMO）规则**

- UMO 改为下拉选择，选项自动取自 AstrBot 现有会话，无需手动填写；新增可选字段 `umo_custom` 供手动兜底。
- `enabled`、`ignore_non_admin` 由三态（`inherit`/`on`/`off`）简化为纯开关，默认分别为 `true`、`false`。
- 会话 `ignore_non_admin` 改为直接覆盖全局值（仅本会话生效）；旧配置里的 `inherit` 仍按「沿用全局」处理。
- 列表合并去掉 `inherit` 档，只保留 `append`（默认，留空即只用全局）与 `replace`。

**其它**

- 配置指纹只跟踪影响判定的 5 个键，改配置后策略重建更轻量。
- 启动日志与判定原因（新增 `session_off`）同步更新。

## 1.0.0

首个版本。

- 全局默认设置 + 会话（UMO）精细化规则，会话规则优先于全局。
- 会话规则三态开关（`inherit` / `on` / `off`）与列表三态合并（`inherit` / `append` / `replace`）。
- 支持忽略非管理员发出的指令，可配置额外管理员与管理员豁免。
- 支持自设忽略指令清单，支持指令组前缀与完整路径匹配，并支持「始终放行」白名单。
- 被忽略时完全静默（不执行、不回复、不触发 LLM），仅在日志记录一条 INFO。
- 两层拦截：静默过滤器注入（压掉框架的权限/参数提示）+ 高优先级兜底监听器。
- 指令名跟随 AstrBot 运行时指令表，后台改名/别名后实时生效。
- 配置热更新：忽略清单与会话规则即时生效，`hard_silence` 变更自动重新注入。
