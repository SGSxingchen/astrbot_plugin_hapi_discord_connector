# 更新日志

## v1.3.4 — LLM 工具会话作用域收紧

1. LLM 工具的 session 解析限制到当前 Discord 窗口已加入的 session，阻断长期记忆里的旧完整 UUID 直通旧会话。
2. `send/stop/archive/delete/leave/status/history` 只允许操作当前窗口 joined session；短 ID、前缀、序号也只在 joined 候选内解析。
3. `create_session` 成功后自动加入当前窗口并刷新 session 缓存，返回文案引导后续省略 `session_id`。
4. `join_session` 改为先刷新并解析 canonical sid 后再请求审批，降低序号/前缀误选风险。
5. 新增 scoped session 回归测试，覆盖旧 UUID 串线、短前缀解析和非 joined 操作拒绝。

## v1.3.3 — agent final 模板回归

1. 修复 `_prepare_final_message` 没有应用 `trigger_message_template` 配置的问题，注入主链的 synthetic user 消息现在会重新走用户配置的模板。
2. 长文本兜底分支也接入模板：模板里的 `{content}` 占位会被替换成"预览 + 文件路径 + 提示"组合段。

## v1.3.2 — DHAPI agent final 注入修复

1. 修复 agent final cached 路径偶发吞正文导致对话历史只剩 `<system_reminder>` 时间戳壳的问题；synthetic event 统一走 fallback 从 0 构造 `DiscordPlatformEvent`，不再复用 base_event 的 `_extras` / `message_obj`。
2. 修复 agent final 临时 md 文件被 `max_content_chars` 截断的问题；磁盘备份文件始终写入完整原文。
3. `_conf_schema.json` 中 agent final 相关配置项的 description / hint 文案重写，明确"主链截断 ≠ 文件保留 ≠ 预览长度"。
4. `agent_final_trigger.py` 删除 `_event_cache` / `_build_from_cached_event` / `_recover_event_context` / `_is_recoverable_context_error`；`remember_event` 保留为兼容 stub。

## v1.1.0 — DHAPI agent final 自动触发主链

1. 新增 `enable_agent_final_trigger` 配置项，支持在 HAPI/Codex assistant final 回包后触发 AstrBot 主链。
2. 新增 `trigger_agents`、`trigger_message_template`、`max_content_chars`、`dedupe_ttl_seconds` 配置项。
3. 触发机制只监听 HAPI 原始 SSE 完成边沿，不修改 Discord adapter，也不关闭 bot-message 过滤。
4. synthetic event 带有 `source=dhapi_agent`、`synthetic=true`、`session_id`、`agent`、`event_id` 标记，并带去重和截断保护。

## v2.0.6 - 新增 `/hapi resume` 指令，用于恢复已经存档的session

## v2.0.5 — Codex 思考深度支持

1. 新增 Codex 会话创建时的思考深度选项（需 HAPI 服务端 >= 0.16.2）

## v2.0.0 大更新 — 支持自然语言操作远程会话

**此版本提供了 Astrbot 原生 Function Calling 能力的集成，现在你可以用自然语言管理远程 vibe 会话了**

利用v1.6.0大版本的会话管理机制，相关 Function Calling 工具将动态选择注册。

如果你在当前群组/私聊窗口没有对 hapi 相关远程服务进行管理，管理相关的工具将不会注册，避免污染上下文

如果与 astrbot 对话的不是管理员，hapi 相关工具完全不会为其注册

1. **新增 LLM 工具支持**：为 Astrbot 提供 10 个工具，实现 AI 代理远程管理 HAPI coding sessions
   - 查询类工具（4个）：获取 session 列表、状态、配置、可用命令
   - 操作类工具（6个）：发送消息、切换 session、创建 session、停止消息、修改配置、执行任意 HAPI 命令
   - 为了管理会话，建议至少激活查询可用命令、执行任意 HAPI 命令两个工具 ( 即 hapi_coding_list_commands 和 hapi_coding_execute_command )，执行 HAPI 命令的工具可以为你主动执行任一 hapi 命令，其它工具的存在仅是为了方便管理和快速调用。
   - 所有操作类工具均复用了审批命令和审批逻辑，需管理员审批，依然支持 `/hapi a` 快捷批准、`/hapi deny` 拒绝，依然支持戳一戳快速批准（QQ NapCat），防止模型呆傻误操作之类的给人添乱

2. **审批机制优化**
   - 序号管理系统：每个待审批请求分配唯一序号，删除后自动回收复用
   - 优化审批通知格式：显示"当前共 x 个待审批，此请求审批序号：x"

## v1.6.0 — 多会话通知管理机制改进

1. 修复 Codex SSE 完成态判定，修复部分情况会出现的codex延迟通知问题

2. 支持多窗口（多会话）推送机制，现在可以借助群聊、私聊、不同管理员账户的对话窗口区分通知消息

### 多会话更新管理机制改进介绍

这是一次兼容性更新，如果你没有这类需求，可以忽略此功能更新，照常使用插件。相关的配置，插件将会自动迁移和兼容

**在不同 AstrBot 会话中（比如 QQ 的私聊、群聊）， session 会话的管理将会互相独立**

根据 AstrBot 的对话窗口 id 进行区分，每个对话窗口只会看到和管理属于自己的 session。

在某个对话窗口使用 `sw` / `create` 命令后，将会自动把对应 session 的通知路由到当前会话。

点击跳转github查看详细图文说明：
https://github.com/LiJinHao999/astrbot_plugin_hapi_connector/blob/master/docs/session-isolation.md



## v1.5.1 — 命令体验优化 & bug修复 & 文件上传支持

1. 新增 `/hapi clean [路径前缀]` 命令，批量清理已归档 sessions
2. SSE 连接支持最大重试次数限制，避免无限重连，并增加了相关配置项
3. 优化所有命令输出格式与提示文本，消除歧义，提升可读性
4. 修复了手机端在开启输入状态感知情况下，napcat发送的心跳消息等空消息导致交互式命令异常退出的问题
5. 支持了 hapi upload 命令，现在可以上传文件了。使用快捷发送时也可以直接在消息中附上图片。

## v1.5.0 — 文件列表 & 文件下载

1. 新增 `/hapi files [关键词]` 命令，搜索远端 session 工作目录下的文件
2. 新增 `/hapi download <路径>` 命令（别名 `dl`），下载远端文件并发送到聊天，支持图片预览
3. 大文件（>10MB）下载前自动弹出确认提示

## v1.4.3

1. 新增 Cloudflare Zero Trust Access 认证配置支持，以便连接公网HAPI服务
2. 新增 CF Access 配置指南文档（含截图）

## v1.4.2

1. 增强了 SSE 连接错误处理的提示逻辑
2. 优化了 Session 列表格式

## v1.4.0 — 交互视觉优化

1. 优化消息输出格式，提升交互可读性：
   - 工具调用提醒统一改为 `🛠️ 工具名: 参数` 格式，替代原 `[Function Calling - 调用 XXX]`，提升直观性
   - `TodoWrite` 工具调用渲染为任务清单，支持 ✅ / 🔄 / ⬜ 状态符号

## v1.3.1

1. 新增上下文压缩支持：检测到 `Prompt is too long` 时复用权限审批流，自动发送 `/compact`，未开启自动审批时推送审批提示；压缩完成后自动发送「继续」恢复会话
2. 修复了session当前上下文过长时导致SSE请求流崩溃的问题

## v1.3.0 — 自动化托管支持

1. 新增自动审批功能：
   - 新增 `auto_approve_enabled` 配置项（默认关闭），开启后 24 小时自动批准所有非 question 权限请求
   - 自动批准触发时，即使 `silence` 模式也会推送 `[自动审批] 已自动批准` 通知
2. 新增 `/hapi remote` 命令，切换当前 session 到 remote 远程托管模式
3. 修复 `/hapi msg` 命令输出内容过多后下次调用失效的问题（超长消息自动按行边界分片发送）
4. 修复 `/hapi msg` 命令无法解析部分消息格式的问题
5. 修复 `silence` 模式下的 TOCTOU 竞态问题（推送前二次检查 `output_level`）

## v1.2.3

1. 新增待审批请求超时提醒功能：
   - 新增 `remind_pending` 配置项（默认关闭），开启后若 pending 请求在指定时间内未被处理，发送一次提醒
   - 新增 `remind_interval` 配置项（默认 180 秒），倒计时内处理完则不提醒
2. `poke_approve` 默认改为开启

## v1.2.1

1. 新增 `AskUserQuestion` 类型权限请求的识别与处理：
   - SSE 推送时自动识别 question 类型，展示问题标题、题目和选项
   - 新增 `/hapi answer [序号]` 命令，交互式逐题回答（支持多问题步进、自定义输入）
   - 新增 `/hapi allow [序号]` 命令，仅批准普通权限请求（跳过 question）
   - `/hapi a` 调整为：先批准所有普通权限请求，再交互式处理所有 question
   - 戳一戳审批与 `/hapi a` 行为一致：批准普通权限请求后交互式处理 question

## v1.2.0 — 基础功能完善

1. 清理了无用 JSON，优化了交互内容输出，debug 输出模式重构为 detail，统一使用语义标签格式推送：
   - `[Message]: AI 回复文本`
   - `[Function Calling - 调用 Bash]: ls -la`
   - `[System]: Context was reset`
   - `[User Input]: 用户消息`
2. 重构了 msg 命令，现在不按条数计算消息，而是按交互轮数（`/hapi msg [轮数]`）
3. 新增了 abort（别名 stop）命令，用于打断会话（`/hapi abort [序号|ID前缀]`）
