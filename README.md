<div align="center">

# HAPI Discord Connector

_✨ HAPI 远程 vibe coding 的 Discord 专用版 ✨_

[![License](https://img.shields.io/badge/License-AGPLv3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0.html)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Author](https://img.shields.io/badge/作者-SGSxingchen-blue)](https://github.com/SGSxingchen)

</div>

---

## ✅ 官方提交信息

- **支持平台**：仅 Discord（`support_platforms: [discord]`）。
- **不支持平台**：QQ、微信、Telegram、飞书等其他 AstrBot 平台不会启用 `/dhapi` 或 `dhapi_coding_*` 工具。
- **受控后台例外**：可选为精确白名单的 AstrBot Scheduler/cron 任务开放 3 个按 `session_id` 直连工具；默认关闭，详见[后台会话直连说明](docs/2026-08-06-background-session-direct-mode.md)。
- **AstrBot 版本**：建议 AstrBot **3.4+**（metadata 声明 `>=3.4.0`）。
- **Python 版本**：**Python 3.10+**。
- **Discord 适配依赖**：依赖 AstrBot 官方 Discord 适配器提供的 Discord SDK；插件代码使用 `discord.ui.View/Button/Select/Modal`，兼容 py-cord / discord.py 风格接口，不额外固定 Discord SDK 版本。
- **仓库**：<https://github.com/SGSxingchen/astrbot_plugin_hapi_discord_connector>

> 本插件是 Discord 专用 UI/路由层；HAPI 后端、AstrBot、Discord Bot 都需要先正常可用。

## 🚀 安装与启用

1. 将本仓库目录放入 AstrBot 的插件目录，例如：

   ```bash
   cd /root/AstrBot/data/plugins
   git clone https://github.com/SGSxingchen/astrbot_plugin_hapi_discord_connector.git
   ```

2. 在 AstrBot 管理面板中启用插件并填写配置：
   - `hapi_endpoint`：HAPI 后端地址，例如 `http://127.0.0.1:3006`。
   - `access_token`：HAPI Access Token；如你的 HAPI 使用 namespace，可填写 `token:namespace`。
   - 如 HAPI 在 Cloudflare Zero Trust 后，可选填 `cf_access_client_id` / `cf_access_client_secret`。

3. 确认 Discord Bot 与 AstrBot Discord 适配器正常工作：
   - Bot 已邀请到目标服务器/频道。
   - Bot 具备发送消息、使用 Slash Command、发送 Embed、处理组件交互（按钮/下拉菜单/Modal）的权限。
   - Slash Command 同步后，在 Discord 输入 `/dhapi` 打开面板。

4. 重载或重启 AstrBot 插件，打开 Discord 执行：

   ```text
   /dhapi
   ```

5. 使用面板选择或创建 HAPI session。通过 `/dhapi` UI 或 `dhapi_coding_create_session` 创建的新 session 会自动捕获并绑定到当前 Discord 窗口；外部 HAPI REST spawn 的 session 没有 owner 时，会回退到主通知窗口/默认窗口接收通知。

## 📦 这是什么？

本插件是 [astrbot_plugin_hapi_connector](https://github.com/LiJinHao999/astrbot_plugin_hapi_connector)（作者 [@LiJinHao999](https://github.com/LiJinHao999)）的 **Discord 专用重构版**。

上游插件覆盖 QQ / 微信 / Telegram 等多平台，使用 `/hapi xxx` 文本子命令完成会话管理、审批、文件操作。本插件**只面向 Discord**，把所有交互改造为 Discord 原生的 **Embed + Button + Select** 卡片，并将入口收敛为唯一的 slash command：

```
/dhapi
```

执行后会打开仅自己可见的 ephemeral 面板，全部操作通过按钮和下拉菜单完成。**所有 `/hapi xxx` 文本子命令在本插件中已废弃**。

> 上游连接的后端服务依旧是 [HAPI](https://github.com/tiann/hapi)，本插件不修改后端协议，仅替换前端交互层。

---

## ✨ 与上游的差异

| 维度 | 上游 hapi_connector | 本插件 hapi_discord_connector |
|------|--------------------|------------------------------|
| 目标平台 | QQ / 微信 / Telegram / Discord 等多平台 | **仅 Discord** |
| 主要入口 | `/hapi list`、`/hapi sw`、`/hapi a` 等数十个文本子命令 | 唯一 slash command `/dhapi` |
| 交互形式 | 文本指令 + 文本回复 | Discord 原生 Embed / Button / Select / Modal |
| 审批流程 | `/hapi a`、`/hapi deny`、戳一戳 | 卡片按钮原地点击，ephemeral 不污染频道 |
| 创建 session | 多步文本向导 | Select 分步选 machine / agent / session_type / yolo / reasoning_effort，仅目录走 Modal |
| LLM 工具命名 | `hapi_coding_*` | `dhapi_coding_*`（与上游隔离，避免双开冲突） |
| 上下文污染 | 文本回复较多 | ephemeral + 卡片，最大限度避免污染聊天上下文 |

---

## 🧩 功能概览

`/dhapi` 打开后的主面板包含：

- **Session 列表**：选择 session 后加入/退出当前窗口订阅、查看状态、停止生成、重新打开 inactive session、删除、归档、返回
- **Codex 会话设置**：在 Session 卡片中切换 Fast/Standard，并调整运行中的推理强度（含 `max`）
- **当前状态**：当前绑定 session 的 flavor / 路径 / 模型 / 权限模式 / 思考状态
- **审批面板**：选择 pending request 后批准、拒绝、批准全部、刷新、返回；单项处理后会保留该请求的完整参数与结果。审批通知优先使用 Embed + 原生 Discord 按钮，可直接批准/拒绝/打开审批面板，异常时保留 pending 并降级提示
  - LLM 发起的“发送消息”和“创建会话”请求会保留 Markdown 正文或以可读字段展示模型、强度与目录，不显示内部字典字符串。
- **创建 session**：Select 分步选 machine / agent / session_type / yolo / reasoning_effort，并在 Modal 中填写目录与可选的 `model`（如 `gpt-5.6-terra`）
- **配置只读页**：查看连接、推送、自动审批等长期配置
- **自动审批开关**：开启后 24 小时自动批准非交互式权限请求
- **危险操作二次确认**：删除、归档需要再点一次确认按钮

通知侧延续上游路由能力：

- 按 Discord 频道（私聊 / 服务器频道）隔离 session 通知
- 支持默认通知频道、按 agent 类型分别绑定默认频道
- session 可被多个 Discord 窗口加入订阅，后续通知会去重后群发到所有已加入窗口
- 操作和订阅脱钩：LLM/按钮可以显式指定 session 操作；加入/退出只影响通知订阅
- SSE 推送级别 silence / simple / summary / detail 与上游一致

---

## 🤖 LLM 工具集成

插件向 AstrBot 注册一组 `dhapi_coding_*` Function Calling 工具，便于在 Discord 中通过自然语言驱动会话管理：

| 工具 | 说明 |
|------|------|
| `dhapi_coding_list_sessions(window="", path="", agent="", joined_only=false)` | 列出 session（支持窗口/路径/agent 过滤；`joined_only=true` 时仅列出当前窗口已加入的 session） |
| `dhapi_coding_get_status(session_id="")` | 获取 session 状态；受控 cron 后台模式必须填写完整 ID 或唯一前缀 |
| `dhapi_coding_message_history(rounds=1, session_id="")` | 查询历史消息；受控 cron 后台模式必须填写完整 ID 或唯一前缀 |
| `dhapi_coding_get_config_status` | 查看插件配置 |
| `dhapi_coding_list_commands` | 列出可用操作（按主题分类） |
| `dhapi_coding_send_message(message, session_id="")` | 向 session 发送消息（需审批；受控 cron 会回原绑定 Discord 窗口审批） |
| `dhapi_coding_join_session(session_id)` | 在当前 Discord 窗口加入 session 订阅（需审批） |
| `dhapi_coding_leave_session(session_id="")` | 从当前 Discord 窗口退出 session 订阅（需审批） |
| `dhapi_coding_create_session` | 创建新 session（需审批） |
| `dhapi_coding_stop_message(session_id="")` | 停止消息生成（需审批） |
| `dhapi_coding_archive_session(session_id="")` | 归档 session（危险，需审批） |
| `dhapi_coding_delete_session(session_id="")` | 删除 session（危险，需审批） |
| `dhapi_coding_change_config` | 修改插件配置 |

`session_id` 不传时只是便捷糖：只有当前 Discord 窗口刚好加入了 1 个 session 才会自动使用它。为避免 LLM 长期记忆中的旧完整 UUID 串到旧 session，`send/status/history/stop/archive/delete/leave` 只会在**当前窗口已加入的 session** 内解析显式参数；短 ID / 前缀 / 序号也只在当前窗口 joined 列表内生效。未加入时请先 `dhapi_coding_list_sessions` + `dhapi_coding_join_session(session_id)`，或创建新 session 后省略 `session_id`。

操作类工具的审批入口与文本流分离：LLM 工具发起操作时会在当前 Discord 窗口发送 **Embed + 原生按钮**，可直接点击“批准 / 拒绝 / 打开审批面板”，也可进入 `/dhapi` 审批页处理。

### 受控 Scheduler/cron 后台模式

AstrBot Scheduler/cron 没有 Discord MessageEvent 上下文时，默认不会看到或调用 DHAPI 工具。管理员可显式开启后台模式，并把**精确的 Scheduler `job_id`** 加入白名单；该任务随后仅能调用 `get_status`、`message_history`、`send_message` 三个工具。

- 后台调用必须提供完整 `session_id` 或唯一前缀，不支持空值、序号、默认 session 或全局搜索。
- 目标必须已持久化绑定到仍在线的 Discord 窗口。无绑定、失效绑定或前缀歧义会拒绝，绝不会回退到默认频道。
- 非 yolo 的续派会只向该 session 持久化 join 顺序中的第一个仍在线 Discord 窗口发送审批卡；所有者可在卡片或 `/dhapi` 审批页处理。
- 无卡直发必须由单独的 yolo 或全局自动审批开关明确允许，且依旧要求 cron 白名单和在线绑定。
- `stop/archive/delete/create/join/leave/配置变更` 等危险或改变绑定的操作不支持后台模式。

完整的安全边界、配置与部署步骤见[受控 cron 后台会话直连](docs/2026-08-06-background-session-direct-mode.md)。

---

### Discord Embed 兼容

LLM 工具审批通知会优先发送 Discord Embed，并附带原生 `discord.ui.View` 按钮（批准、拒绝、打开/刷新审批面板），不是只提示用户打开 `/dhapi`。插件层提供本地兼容组件与发送补丁，避免 AstrBot DiscordEmbed 包装类在热重载或字段定义不一致时导致 `no field "title"`。

如果 LLM 工具审批的 `Embed + 按钮` 发送失败，插件会先尝试 `Embed-only`，再按 `auto_approve_enabled` 决定是否自动批准或保留 pending 供 `/dhapi` 面板处理。SSE 通知侧不会再在 Embed 失败后追加纯文本降级，以避免同一事件出现 Embed + 文本双推；失败原因会记录到日志。

---

## ⚙️ 配置项

在 AstrBot 管理面板中填写：

### 连接与认证

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `hapi_endpoint` | HAPI 服务地址，如 `http://0.0.0.0:3006` | |
| `access_token` | HAPI Access Token，支持 `token:namespace` 格式 | |
| `proxy_url` | 代理地址，支持 `socks5h://` 和 `http://` | 空 |
| `cf_access_client_id` | Cloudflare Zero Trust Service Token 的 Client ID | 空 |
| `cf_access_client_secret` | Cloudflare Zero Trust Service Token 的 Client Secret | 空 |
| `jwt_lifetime` | JWT 有效期（秒） | 900 |
| `refresh_before_expiry` | JWT 提前刷新时间（秒） | 180 |

### 推送与交互

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `output_level` | SSE 推送级别：`silence` / `simple` / `summary` / `detail` | simple |
| `summary_msg_count` | summary 级别显示的 agent 消息条数 | 5 |

### 自动审批

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `auto_approve_enabled` | 自动审批开关：开启后 24 小时生效，自动批准非交互式权限请求 | 关闭 |
| `background_direct_enabled` | 开启受控 cron 按 session_id 直连；没有白名单仍全部拒绝 | 关闭 |
| `background_allowed_cron_job_ids` | 允许后台直连的 Scheduler 精确 `job_id` 列表 | 空列表 |
| `background_direct_allow_yolo_send` | 已确认 `permissionMode=yolo` 时允许受控 cron 直接续派 | 关闭 |
| `background_direct_allow_global_auto_approve_send` | 已开启全局自动审批时允许受控 cron 直接续派 | 关闭 |

> 自动审批不再使用开始/结束时间窗口；开启就是 24 小时生效。需要用户回答的 AskUserQuestion 类请求仍保留在审批面板中处理。

> 后台直连的两个“直接续派”开关不会自动随 `auto_approve_enabled` 或 session yolo 状态生效，必须另外开启；请只对已审查的 Scheduler `job_id` 使用。


后端连接、SSE 推送级别、绑定优先级等行为与上游 [hapi_connector](https://github.com/LiJinHao999/astrbot_plugin_hapi_connector) 完全一致，详细原理参考上游文档。

---

## 📁 插件结构

```
astrbot_plugin_hapi_discord_connector/
├── main.py                 # 插件生命周期、LLM 工具注册、/dhapi 入口
├── discord_ui.py           # Discord 原生 View / Button / Select / Modal UI
├── create_wizard.py        # 创建 session 分步向导（Select + Modal）
├── llm_integration.py      # dhapi_coding_* Function Calling 工具
├── notification_manager.py # SSE 通知推送（Embed）
├── sse_listener.py         # HAPI SSE 监听
├── session_ops.py          # Session CRUD（包括归档/删除）
├── approval_ops.py         # 审批业务逻辑
├── pending_manager.py      # 待审批请求管理
├── binding_manager.py      # 频道与 session 绑定
├── state_manager.py        # 用户/频道状态
├── hapi_client.py          # 异步 HAPI HTTP 客户端 + JWT 刷新
├── cf_access.py            # Cloudflare Zero Trust Service Token 认证
├── file_ops.py             # 文件查询/上传/下载
├── formatters.py           # Embed 格式化
├── constants.py            # 常量定义
├── _conf_schema.json       # 插件配置 schema
└── metadata.yaml           # 插件元信息
```

---

## 🙏 致谢与上游来源

- 本插件 fork 并重构自 [@LiJinHao999](https://github.com/LiJinHao999) 的 [astrbot_plugin_hapi_connector](https://github.com/LiJinHao999/astrbot_plugin_hapi_connector)，所有后端协议、绑定路由、SSE 推送、审批逻辑均沿用其设计，特此致谢。
- [HAPI](https://github.com/tiann/hapi) — 后端服务，由 [@tiann](https://github.com/tiann) 开发
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 跨平台聊天机器人框架

本插件遵循上游协议（AGPLv3）开源。

---

## 👥 贡献

- 🌟 Star 本项目
- 🐛 提交 Issue 反馈 Discord UI 相关问题
- 💡 提出新的 Discord 原生交互方案
- 🔧 提交 Pull Request

> 与上游 hapi_connector 的多平台/文本指令相关 issue，请优先到上游仓库提交。
