# HAPI 上游同步说明

## 本次同步目标

本插件保留 Discord 专用的 `/dhapi` 卡片交互与 `dhapi_coding_*` 工具，不直接引入通用上游的多平台文本指令、Focus 模式和 WebUI。同步重点是 HAPI 后端协议变化在 Discord 消息流中的兼容性。

## 已同步内容

- 对齐 Discord 专用上游 v1.3.8：未加入 session 时仍可向模型提供发送消息工具的定义，执行阶段继续校验当前 Discord 窗口的订阅范围。
- 兼容 HAPI 0.24+ 的 Codex 子代理内部事件：`agent-run-trace`、`agent-run-update` 被识别为内部进度，不再发送成无意义的频道消息。
- 保留 SSE 低频自动恢复：达到重连上限后仍会自动尝试重连，恢复后主动通知。
- 将新版 HAPI 会话能力适配到 Discord 卡片：inactive session 可重新打开；Codex 可切换 Fast/Standard 与运行中推理强度，支持 `max`。
- 审批完成后保留请求快照，展示原请求参数和处理结果，避免待审批队列清理后丢失上下文。
- 创建 session 支持显式 `model`，与 `modelReasoningEffort` 分离传给 HAPI；例如 `model=gpt-5.6-terra` 与 `modelReasoningEffort=max`。

## 升级建议

建议将 HAPI 升级到 0.24.0 或更高版本：新版后端已修复内部子代理事件泄漏到聊天消息流的问题。本插件的过滤是兼容保护层，升级后仍应保留。

## 验收方法

1. 重载 AstrBot 插件后，在 Discord 打开 `/dhapi`。
2. 运行会产生 Codex 子代理活动的任务。
3. 确认频道不再出现 `[Message]: [agent-run-trace]` 或 `[Message]: [agent-run-update]`。
4. 确认普通回复、工具调用、权限审批和完成通知仍正常投递。
