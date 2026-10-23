# AstrBot 指令守卫插件

`astrbot_plugin_command_guard` 是 AstrBot 的指令访问控制插件。它按全局规则或指定会话的规则忽略指令：被忽略的指令不会执行、不会产生回复，也不会触发 LLM 请求，仅在日志中记录一条信息。

## 功能

- 提供全局默认设置，并支持为单个会话（UMO）单独配置规则，会话规则的优先级高于全局设置。
- 可选择忽略非管理员发出的指令。
- 可自定义忽略指令清单，并支持始终放行清单。
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

插件默认关闭，安装后不改变任何现有行为。在 WebUI 的插件配置页开启总开关后生效。

### 全局设置

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `enabled` | bool | `false` | 总开关。 |
| `ignore_non_admin` | bool | `false` | 忽略非管理员发出的指令。 |
| `ignored_commands` | list | `[]` | 要忽略的指令，如 `帮助`、`群管`。可带 `/` 前缀。 |
| `exempt_commands` | list | `[]` | 始终放行的指令，优先级高于忽略规则。 |
| `admin_bypass` | bool | `true` | 管理员不受忽略规则影响。关闭后忽略清单对管理员同样生效。 |
| `extra_admins` | list | `[]` | 额外管理员，填写 `UID` 或 `平台名:UID`。 |
| `trust_bot_self` | bool | `true` | 放行机器人自身发出的事件。 |
| `hard_silence` | bool | `true` | 静默拦截，同时屏蔽 AstrBot 自带的权限与参数提示。 |
| `log_ignored` | bool | `true` | 忽略时在日志中记录一条 INFO。 |
| `session_rules` | template_list | `[]` | 会话规则，见下。 |

### 会话规则

在 `session_rules` 中为指定会话单独配置规则。多条规则按顺序匹配，首条命中的规则生效。

| 字段 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `umo` | string | `""` | 会话标识，如 `aiocqhttp:GroupMessage:123456`。支持通配符 `aiocqhttp:GroupMessage:*`；仅填数字时匹配会话标识末段（群号或用户号）。 |
| `enabled` | select | `inherit` | 该会话是否启用守卫：`inherit` / `on` / `off`。 |
| `ignore_non_admin` | select | `inherit` | 该会话是否忽略非管理员：`inherit` / `on` / `off`。 |
| `ignored_mode` | select | `inherit` | 忽略清单的处理方式：`inherit` 沿用全局，`append` 追加，`replace` 覆盖。 |
| `ignored_commands` | list | `[]` | 该会话的忽略清单。 |
| `exempt_mode` | select | `inherit` | 放行清单的处理方式，语义同 `ignored_mode`。 |
| `exempt_commands` | list | `[]` | 该会话的放行清单。 |

### 配置示例

仅允许管理员使用指令，同时保留帮助类指令给所有人：

```json
{
  "enabled": true,
  "ignore_non_admin": true,
  "exempt_commands": ["帮助", "help", "sid"]
}
```

忽略全部群管指令，但在指定群中仅忽略抽卡指令：

```json
{
  "enabled": true,
  "ignored_commands": ["群管"],
  "session_rules": [
    {
      "umo": "aiocqhttp:GroupMessage:123456",
      "ignored_mode": "replace",
      "ignored_commands": ["抽卡"]
    }
  ]
}
```

## 判定顺序

```
未启用                                  → 放行
管理员且 admin_bypass 开启               → 放行
机器人自身事件且 trust_bot_self 开启     → 放行
命中 exempt_commands                    → 放行
ignore_non_admin 开启且发送者非管理员    → 忽略
命中 ignored_commands                   → 忽略
其余                                    → 放行
```

指令匹配规则：

- 仅处理被唤醒且以指令形式触发的消息，普通聊天不受影响。
- 指令名与 AstrBot 指令表一致，包含别名与父指令组前缀。
- 配置项与指令名完全相等，或以「配置项 + 空格」开头即视为命中，因此填写指令组名可覆盖其子指令。
- 指令名区分大小写，配置项首尾空白与 `/` 前缀会被忽略。

## 注意事项

- LLM 工具直接调用指令（如 `astrbot_plugin_llm_executor`）不经过消息管道，不受本插件约束。
- 仅覆盖通过 `CommandFilter` 与 `CommandGroupFilter` 注册的指令；正则触发、事件监听器与普通对话不视为指令。
- 未被任何插件注册的指令无法识别，仍按 AstrBot 原有逻辑处理。
- 群主、群管理员等平台身份不等同于 AstrBot 管理员，如有需要请填入 `extra_admins`。
- 关闭 `hard_silence` 后，被忽略的管理员专属指令仍可能收到 AstrBot 的权限提示。

## 许可

MIT
