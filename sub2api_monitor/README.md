# Sub2API 订阅监控

`sub2api_monitor` 是 Sirius Pulse 的后台轮询插件，用于监控：

- 可售订阅上架、下架和内容更新；
- 分组倍率新增、移除和变化；
- 变化发生后向配置的群聊发送主动通知。

插件首轮只建立快照，不会把现有订阅当成刚上架。快照按 Persona 存入其独立
`PluginDataStore`；账号、密码、access token 和 refresh token 不会写入快照。

## 配置

在 Sirius Pulse WebUI 的插件设置中配置非敏感运行参数，并可对照
[`settings.example.json`](settings.example.json) 填写。**所有接口路径都在运行时配置；
`subscriptions_path` 和 `group_rates_path` 是必填的监控路径，插件不内置或写死任何
监控端点。** 请按自己的 Sub2API 部署填写它们：

| 字段 | 说明 | 示例 |
|---|---|---|
| `base_url` | 中转站页面或站点地址；插件只取其 origin | `https://sub2api.example.invalid/keys` |
| `api_base_path` | API 根路径；接口路径相对于它解析 | `/api/v1` |
| `login_path` | 登录接口（相对 API 根路径、完整 API 路径或同源 URL） | `/auth/login` |
| `refresh_path` | Token 刷新接口；留空则过期后重新登录 | `/auth/refresh` |
| `logout_path` | 当前会话注销接口；卸载时尽力调用 | `/auth/logout` |
| `subscriptions_path` | 可售订阅监控接口；**运行时必填，无内置默认端点** | `/your/subscriptions-path` |
| `group_rates_path` | 分组倍率监控接口；**运行时必填，无内置默认端点** | `/your/group-rates-path` |
| `timezone` | GET 请求的 `timezone` 查询参数 | `Asia/Shanghai` |
| `poll_seconds` | 轮询间隔，最小 30 秒 | `300` |
| `timeout` | 单次请求超时秒数 | `20` |
| `notify_group_ids` | **显式通知允许列表**；只有列出的群号会收到主动通知，空列表不启动后台轮询 | `123456789, 987654321` |
| `adapter_type` | 主动通知平台类型；留空由引擎选择 | `napcat` |
| `run_on_persona` | **必须明确填写唯一负责后台和 `/sub2api poll` 的 Persona 名称**；留空会禁用后台与手动轮询 | `sub2api-poller` |

表中的路径仅为运行时占位示例，不是插件固定的完整接口 URL。接口字段也可填写
完整 URL，但插件会校验它必须与 `base_url` 同源，避免 Bearer Token 被发送到第三方
域名。生产环境要求 HTTPS；仅本机调试可开启 `allow_insecure_http`。

### 登录凭据

`SUB2API_EMAIL` 和 `SUB2API_PASSWORD` 是本插件支持的凭据机制。请在启动 Sirius
Pulse 的进程环境中同时提供它们：

```powershell
$env:SUB2API_EMAIL = "your-account@example.com"
$env:SUB2API_PASSWORD = "your-password"
uv run python main.py run
```

**不要通过 WebUI 或任何插件 settings 填写、保存或分发密码。** 模板刻意不含
`email` 或 `password` 字段，以免凭据进入本地 `plugins/_config.json`。

## 命令

插件命令仅开发者可用：

- `/sub2api status`：查看配置、快照数量和上次轮询时间；
- `/sub2api poll`：由 `run_on_persona` 指定的人格立即轮询，并按正常规则发送变化通知；
- `/sub2api subscriptions`：查看当前接口返回的可售订阅；
- `/sub2api rates`：查看当前接口返回的分组倍率；
- `/sub2api reset`：删除快照，使下次轮询重新静默初始化。

`run_on_persona` 为空时，不会创建后台任务，`/sub2api poll` 也不能执行；请为唯一
轮询 Persona 填写其实际名称，而不是让每个人格各自轮询。

## 变化与失败语义

- 记录优先使用 `id`、`plan_id`、`group_id` 等稳定字段作为身份；
- 响应支持 `data.plans`、`data.subscriptions`、`data.items`、倍率列表和倍率映射等常见形状；
- 两个接口独立处理，一个失败时另一个仍可更新；
- 第一次快照以及监控来源变更（站点、账号、API 根/监控路径或时区）都会静默建立新来源快照，不通知历史内容；
- `notify_group_ids` 是显式允许列表；只有其中的群参与通知和确认，不能作为任意群号的广播目标；
- 每个变化按群保存通知 ACK 状态。投递失败或未确认时，会保留已有的逐群 ACK，失败/未确认的群保持未 ACK；下次轮询只重试尚未确认的投递，已确认的群不会重复发送；
- 仅当该变化对允许列表中的群均已确认后，才提交对应快照。框架的“确认”可能只是适配器或平台已受理/确认发送，并不表示最终用户已经阅读；
- 多 Persona 部署必须配置唯一的 `run_on_persona`，避免重复轮询和通知；留空会同时禁用后台与 `/sub2api poll`；
- HTTP 401 会优先刷新 token，并只重试原请求一次，不会无限重试；
- HTTP 响应中的 token、password、api_key 等字段会在持久化和命令输出前脱敏。

## 测试

测试全部使用 `httpx.MockTransport` 和内存假客户端，不访问真实中转站：

```bash
uv run pytest plugins/tests/test_sub2api_monitor.py -q
```
