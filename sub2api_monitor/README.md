# Sub2API 订阅监控

`sub2api_monitor` 是 Sirius Pulse 的后台轮询插件，用于监控：

- 可售订阅上架、下架和内容更新；
- 分组倍率新增、移除和变化；
- 变化发生后向配置的群聊发送主动通知。

插件首轮只建立快照，不会把现有订阅当成刚上架。快照按 Persona 存入其独立
`PluginDataStore`；账号、密码、access token 和 refresh token 不会写入快照。

## 配置

在 Sirius Pulse WebUI 的插件设置中配置 `sub2api_monitor`。可对照
[`settings.example.json`](settings.example.json) 填写；模板不含账号密码。接口地址没有
写死在插件源码中，每个 Sub2API 部署都可以覆盖下列字段：

| 字段 | 说明 | 示例 |
|---|---|---|
| `base_url` | 中转站页面或站点地址；插件只取其 origin | `https://mollycloud.cn/keys` |
| `api_base_path` | API 根路径 | `/api/v1` |
| `login_path` | 登录接口（相对 API 根路径、完整 API 路径或同源 URL） | `/auth/login` |
| `refresh_path` | Token 刷新接口；留空则过期后重新登录 | `/auth/refresh` |
| `logout_path` | 当前会话注销接口；卸载时尽力调用 | `/auth/logout` |
| `subscriptions_path` | 可售订阅接口，必填 | `/payment/checkout-info` |
| `group_rates_path` | 分组倍率接口，必填 | `/groups/rates` |
| `timezone` | GET 请求的 `timezone` 查询参数 | `Asia/Shanghai` |
| `poll_seconds` | 轮询间隔，最小 30 秒 | `300` |
| `timeout` | 单次请求超时秒数 | `20` |
| `notify_group_ids` | 主动通知目标群号列表；为空时不启动后台轮询 | `123456789, 987654321` |
| `adapter_type` | 主动通知平台类型；留空由引擎选择 | `napcat` |
| `run_on_persona` | 可选：只让指定人格执行后台轮询 | `alice` |

以上路径示例是**运行时配置**，不是插件内固定的完整接口 URL。也可以给接口字段
填写完整 URL，但插件会校验它必须与 `base_url` 同源，避免 Bearer Token 被发送到
第三方域名。生产环境要求 HTTPS；仅本机调试可开启 `allow_insecure_http`。

### 登录凭据

推荐在启动 Sirius Pulse 的进程环境中提供凭据：

```powershell
$env:SUB2API_EMAIL = "your-account@example.com"
$env:SUB2API_PASSWORD = "your-password"
uv run python main.py run
```

也可以在 WebUI 中填写 `email` 和 `password`。请注意 WebUI 的插件设置最终保存在
本地 `plugins/_config.json`；若不希望密码落盘，请保持这两个设置为空并使用环境变量。

## 命令

插件命令仅开发者可用：

- `/sub2api status`：查看配置、快照数量和上次轮询时间；
- `/sub2api poll`：立即轮询，并按正常规则发送变化通知；
- `/sub2api subscriptions`：查看当前接口返回的可售订阅；
- `/sub2api rates`：查看当前接口返回的分组倍率；
- `/sub2api reset`：删除快照，使下次轮询重新静默初始化。

## 变化与失败语义

- 记录优先使用 `id`、`plan_id`、`group_id` 等稳定字段作为身份；
- 响应支持 `data.plans`、`data.subscriptions`、`data.items`、倍率列表和倍率映射等常见形状；
- 两个接口独立处理，一个失败时另一个仍可更新；
- 首次同步及切换站点、账号或接口后只静默建立新来源快照；
- 通知发送成功后才提交对应快照，发送失败会在下次轮询重试该差异；
- 多 Persona 部署建议配置 `run_on_persona`，避免重复轮询和通知；
- HTTP 401 会优先刷新 token，并只重试原请求一次，不会无限重试；
- HTTP 响应中的 token、password、api_key 等字段会在持久化和命令输出前脱敏。

## 测试

测试全部使用 `httpx.MockTransport` 和内存假客户端，不访问真实中转站：

```bash
uv run pytest plugins/tests/test_sub2api_monitor.py -q
```
