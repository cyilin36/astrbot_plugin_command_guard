# AstrBot 指令守卫插件

`astrbot_plugin_command_guard` 是 AstrBot 的指令访问控制插件。它按全局设置或指定会话的规则忽略指令：被忽略的指令不会执行、不会产生回复，也不会触发 LLM 请求，仅在日志中记录一条信息。

## 功能

- 全局设置：可忽略非管理员发出的指令，可自设忽略清单与始终放行清单。
- 会话（UMO）规则：为单个会话单独开关守卫、单独设置忽略非管理员与两个清单；UMO 从 AstrBot 现有会话中下拉选择，无需手填。
- 支持指令组：填一个组名（如 `cr`）即屏蔽整组，包括 `cr`、`cr list`、`cr lora`。
- 命中即终止：判定为忽略后事件在唤醒检查阶段就被终止，不再进入后续阶段，也不会有任何回复。
- 管理员始终放行，直接复用 AstrBot 自带的 `admins_id`，不需要额外配置。
- 机器人自身发出的事件始终放行，交由 AstrBot 自行处理。
- 被忽略的指令必然在日志中留下一条 INFO，没有开关能关掉它。
- 指令名取自 AstrBot 运行时指令表，后台修改指令名或别名后无需重启。

## 环境要求

- AstrBot 4.25.0 及以上版本。
- 无第三方依赖。

## 安装

将本目录放入 AstrBot 的插件目录，目录名须为 `astrbot_plugin_command_guard`：

```bash
cd AstrBot/data/plugins/
git clone https://github.com/cyilin36/astrbot_plugin_command_guard.git
```

随后重启 AstrBot，或在 WebUI 插件页重新加载插件。

## 配置

插件常驻运行，没有总开关。默认配置下两个清单为空、忽略非管理员关闭，因此不会拦截任何指令。

### 全局设置

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `ignore_non_admin` | bool | `false` | 忽略非管理员发出的指令。管理员由 AstrBot 的 `admins_id` 判定。 |
| `ignored_commands` | list | `[]` | 要忽略的指令，如 `帮助`、`群管`。可带 `/` 前缀。填指令组名即屏蔽整组。 |
| `exempt_commands` | list | `[]` | 始终放行的指令，优先级最高，会压过「忽略非管理员」与忽略清单。 |
| `hard_silence` | bool | `true` | 彻底静默，同时屏蔽 AstrBot 自带的权限与参数提示。 |
| `session_rules` | template_list | `[]` | 会话规则，见下。 |

### 会话规则

在 `session_rules` 中为指定会话单独配置规则，规则**只在该会话内生效**。多条规则按顺序匹配，首条命中的规则生效。

| 字段 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `umo` | string | `""` | 会话标识，选项来自 AstrBot 现有会话（运行时自动补齐，刷新页面即可看到最新列表）；还没有任何会话记录时退化为普通输入框。 |
| `umo_custom` | string | `""` | 可选，手动填写 UMO；非空时优先于 `umo`。支持通配符 `aiocqhttp:GroupMessage:*`，仅填数字时匹配会话标识末段（群号或用户号）。 |
| `enabled` | bool | `true` | 该会话是否启用守卫。关闭后本插件完全不处理该会话，指令直接交给 AstrBot。 |
| `ignore_non_admin` | bool | `false` | 该会话是否忽略非管理员指令。开/关都会覆盖全局设置，但仅在本会话生效。 |
| `ignored_mode` | select | `append` | 忽略清单的处理方式：`append` 追加（全局 + 本会话），`replace` 覆写（只用本会话）。 |
| `ignored_commands` | list | `[]` | 该会话的忽略清单。追加且留空时等同于只用全局清单。 |
| `exempt_mode` | select | `append` | 放行清单的处理方式，语义同 `ignored_mode`。 |
| `exempt_commands` | list | `[]` | 该会话的放行清单。 |

### 配置示例

仅允许管理员使用指令，同时保留帮助类指令给所有人：

```json
{
  "ignore_non_admin": true,
  "exempt_commands": ["帮助", "help", "sid"]
}
```

忽略全部群管指令，但指定群覆写为只忽略抽卡；同时在另一个群里彻底关掉守卫：

```json
{
  "ignored_commands": ["群管"],
  "session_rules": [
    {
      "umo": "aiocqhttp:GroupMessage:123456",
      "ignored_mode": "replace",
      "ignored_commands": ["抽卡"]
    },
    {
      "umo": "aiocqhttp:GroupMessage:654321",
      "enabled": false
    }
  ]
}
```

## 判定顺序

```
该会话关闭守卫                        → 放行（完全不处理，交给 AstrBot）
管理员                               → 放行
机器人自身事件                        → 放行
命中放行清单                          → 放行
忽略非管理员开启且发送者非管理员        → 忽略
命中忽略清单                          → 忽略
其余                                 → 放行
```

指令匹配规则：

- 仅处理被唤醒且以指令形式触发的消息，普通聊天不受影响。
- 指令名与 AstrBot 指令表一致，包含别名与父指令组前缀。
- 配置项与指令名完全相等，或以「配置项 + 空格」开头即视为命中，因此填写指令组名可覆盖其子指令。
- 指令名不区分大小写，配置项首尾空白与 `/` 前缀会被忽略。

### 指令组

AstrBot 的指令组（`@filter.command_group` 注册，如 `cr`）会展开成组名与子指令两套 handler。本插件按「前缀 + 空格」匹配，因此：

| 忽略清单里填 | 会被忽略 | 不会被忽略 |
| :--- | :--- | :--- |
| `cr` | `cr`、`cr list`、`cr lora`、`cr 任意子指令` | `抽卡`、`crx` |
| `cr list` | `cr list`（含带参数形式） | `cr`、`cr lora` |

只输入组名（如裸 `cr`）时，AstrBot 原生会回复「参数不足 + 指令树」。被忽略后这条提示同样不会发出。

## 命中之后

判定为忽略时，守卫在唤醒检查阶段就调用 `event.stop_event()` 并让指令 handler 全部跳过，因此：

- 指令 handler 不会执行，不会有任何回复；
- 不进入限流、内容安全、预处理等后续阶段；
- 不触发 LLM 请求；
- 其它监听所有消息的插件监听器也不会被调用（事件在唤醒阶段就被终止）；
- 只写一条 INFO 日志。

这与「机器人根本没收到这条消息」的观感一致。唯一区别是日志里会留下记录。

关闭 `hard_silence` 后只剩兜底监听器，它在处理阶段才终止事件：LLM 仍不会被触发，但限流、内容安全、预处理已经跑过，其它非指令监听器也可能已被调用。

## 日志

每次忽略都会输出一条 INFO：

```
[cmd_guard] 已忽略 | umo=aiocqhttp:GroupMessage:123456 sender=张三/10001 cmd=帮助 reason=listed(指令在忽略列表) scope=session(rule#1) matched=['帮助']
```

该记录是固定行为，没有配置项可以关闭。

## 注意事项

- LLM 工具直接调用指令（如 `astrbot_plugin_llm_executor`）不经过消息管道，不受本插件约束。
- 仅覆盖通过 `CommandFilter` 与 `CommandGroupFilter` 注册的指令；正则触发、事件监听器与普通对话不视为指令。
- 未被任何插件注册的指令无法识别，仍按 AstrBot 原有逻辑处理。
- 群主、群管理员等平台身份不等同于 AstrBot 管理员；本插件只认 AstrBot 的 `admins_id`。
- 关闭 `hard_silence` 后，被忽略的管理员专属指令仍可能收到 AstrBot 的权限提示，裸指令组名也仍会收到「参数不足」提示。
- 会话规则里 `ignore_non_admin` 采用「显式覆盖」语义：一旦给某个会话建了规则，该会话的这项设置就由这条规则决定。

## 许可

MIT
