# 受控 cron 后台会话直连

## 适用范围

AstrBot Scheduler/cron 触发的是 `cron` 事件，不是 Discord 入站消息。本模式允许**已由管理员配置授权的单个 cron 任务**按指定 HAPI session 进行：

- `dhapi_coding_get_status`
- `dhapi_coding_message_history`
- `dhapi_coding_send_message`

Discord 内原有的工具行为没有改变：仍只能在当前窗口已加入的 session 范围内解析参数。

## 安全边界

1. 默认关闭。`background_direct_enabled=true` 仍不足以放行，cron 的数据库任务 ID 必须精确出现在 `background_allowed_cron_job_ids` 中。
2. 后台工具必须填写 `session_id`；只能使用完整 ID 或唯一前缀，不支持空值、列表序号、默认 session 或全局猜测。
3. 目标 session 必须存在于持久化的 `session_id -> Discord UMO` 绑定，且该 UMO 对应当前仍在线的 Discord adapter。无绑定、旧绑定或歧义会明确拒绝。
4. 多窗口加入同一 session 时，审批单播到**持久化 join 顺序中第一个仍在线的 Discord 窗口**。普通 SSE 通知的既有多窗口广播策略不变。
5. 后台续派默认需要人工审批。审批卡固定发往上述绑定窗口；投递失败、拒绝、取消或 60 秒超时均不会发送 HAPI 消息。
6. 只有同时满足受控 cron 白名单、在线 Discord 绑定和下列独立开关时才允许无卡直发：
   - `background_direct_allow_yolo_send=true` 且 HAPI session 详情的 `permissionMode` 精确为 `yolo`；或
   - `background_direct_allow_global_auto_approve_send=true` 且现有 `auto_approve_enabled=true`。
7. `stop`、`archive`、`delete`、`create`、`join`、`leave`、配置变更和旧命令入口不支持后台模式，继续只接受 Discord 上下文。

## 配置示例

先在 Scheduler 创建目标任务并取得其 `job_id`，再由管理员在插件配置中填写：

```json
{
  "background_direct_enabled": true,
  "background_allowed_cron_job_ids": ["你的 Scheduler job_id"],
  "background_direct_allow_yolo_send": false,
  "background_direct_allow_global_auto_approve_send": false
}
```

不需要无人值守续派时，两个 `*_send` 开关应保持 `false`。任务提示词中必须写入完整 session ID 或足够长且唯一的前缀，例如：

```text
使用 dhapi_coding_get_status(session_id="9484eecd-cd21-4890-8bb6-f0c9eb0cf55a")。
随后读取历史并在需要续派时调用 dhapi_coding_send_message，继续使用相同 session_id。
```

## 部署

更新插件文件后，在 AstrBot 管理界面热重载该插件，或重启 AstrBot 服务。首次启用前确认目标 session 已通过 `/dhapi` 或 Discord LLM 工具加入过原频道；后台模式不会自动创建绑定。
