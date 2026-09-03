# Sub2API 多站监控

`sub2api_monitor` 是 Sirius Pulse 的多站点后台轮询插件，用于监控：

- 多个 Sub2API 站点的可售订阅上架、下架和内容更新；
- 各站点的分组倍率新增、移除和变化；
- 按站点隔离账号、快照和逐群通知 ACK；
- 为变化通知和多站运行状态生成可选的本地可视化图。

每个站点的第一次成功同步只建立快照，不会把现有内容当成新变化；站点身份来源发生
变化时也会静默建立新快照。快照按 Persona 存入独立 `PluginDataStore`，账号、密码、
access token 和 refresh token 不会写入快照。

## 配置

需要 Sirius Pulse `1.3.0` 或更高版本。在 WebUI 的“插件 → Sub2API 多站监控 → 参数配置”中维护非敏感运行参数；插件 `0.3.0` 会显示中文分区和站点卡片，无需手工编辑 Schema，也无需为每次部署修改插件源码。也可对照 [`settings.example.json`](settings.example.json) 检查保存结果。

推荐配置流程：

1. 在“监控站点”分区点击“添加 Sub2API 站点”；
2. 填写稳定且唯一的 `id`、可读的 `display_name` 和部署时站点地址；
3. 展开会话接口，按自己的部署填写登录、刷新与注销路径；
4. 在“监控接口与网络”填写**自己的**订阅、倍率路径；界面中的占位路径不是内置接口；
5. 配置站点专属群及是否继承全局群，随后填写唯一 `run_on_persona`；
6. 保存后，按站点 ID 将两项派生凭据注入实际 Persona Worker 环境，再重载插件或重启该 Persona。

卡片标题优先使用 `display_name`，为空时回退稳定 `id`；卡片角标始终显示 ID，状态开关只控制常规轮询。修改显示名称不会改变凭据变量或状态命名空间。删除最后一张卡片会保存显式空数组，即禁用全部站点而不回退旧版单站配置。

WebUI 不提供邮箱、密码或 token 输入框，也不会读取这些值。站点 ID 的 `identity: true`、字段类型、必填和数值边界由插件作者声明，部署者不需要也不能配置。可视化 Schema 只控制标签与布局，不会进入 `plugins/_config.json`。

推荐使用 `sources` 数组；每项代表一个独立认证、独立状态的 Sub2API 站点。

### `sources` 站点字段

| 字段 | 说明 |
|---|---|
| `id` | 必填且唯一；以小写字母开头，只能含小写字母、数字和下划线，最长 32 位；`all` 是保留字 |
| `display_name` | 通知、状态和图表中的显示名称；留空时使用 `id`，不参与凭据变量或状态键派生 |
| `enabled` | 是否参与常规轮询与选择，默认 `true` |
| `base_url` | 必填；站点页面或地址，插件只使用其 origin |
| `api_base_path` | API 根路径，默认 `/api/v1` |
| `login_path` | 登录接口路径；相对 `api_base_path` 解析，也可使用同源完整 URL |
| `refresh_path` | Token 刷新接口；留空时 token 失效后重新登录 |
| `logout_path` | 当前会话注销接口；卸载或配置变化时尽力调用 |
| `subscriptions_path` | **运行时必填**的订阅监控路径；没有内置或硬编码端点 |
| `group_rates_path` | **运行时必填**的分组倍率监控路径；没有内置或硬编码端点 |
| `timezone` | GET 请求的 `timezone` 查询参数；留空则不发送 |
| `timeout` | 单次请求超时，范围 1–300 秒 |
| `allow_insecure_http` | 仅允许 `localhost`、`127.0.0.1` 或 `::1` 调试时使用 HTTP |
| `inherit_notify_group_ids` | 是否继承顶层全局通知群允许列表，默认 `true` |
| `notify_group_ids` | 本站专属通知群允许列表 |

所有接口路径都是运行时配置。示例中的 `/your/subscriptions-path` 和
`/your/group-rates-path` 只是占位符，必须替换为自己部署的实际路径；插件源码不绑定
任何站点或监控端点。接口字段可填写相对路径、API 根路径下的完整路径或同源完整
URL，但不能越过 `api_base_path` 或把 Bearer Token 发送到其他 origin。

### 全局字段

| 字段 | 说明 |
|---|---|
| `poll_seconds` | 所有站点共用的轮询间隔，范围 30–86400 秒 |
| `notify_group_ids` | 全局显式通知群允许列表；是否被某站继承由该站配置决定 |
| `adapter_type` | 主动通知平台类型；留空时由引擎选择 |
| `run_on_persona` | **唯一负责全部站点后台轮询和有状态命令的 Persona 名称**；留空禁用后台与手动轮询 |
| `visual_report_enabled` | 是否为自动变化通知尝试生成 Playwright 图片；失败时仍发送权威文字通知 |

`sources` 的存在具有明确语义：

- 未出现 `sources` 键：进入旧版单站兼容模式；
- `sources` 为非空数组：常规轮询只选择其中 `enabled: true` 的站点；停用项仍必须通过字段与 URL 安全校验；
- **显式配置 `"sources": []`：禁用全部站点，不回退读取旧版顶层站点字段。** 状态不会被删除；指定 Persona 上的 `/sub2api poll` 不会发起站点请求或通知。

`run_on_persona` 仍需明确填写唯一 Persona。空值不会创建后台任务，`/sub2api poll`
也不能执行；不要让多个人格分别承担同一组站点的轮询。

## 多账号凭据环境变量

凭据变量名由 `sources[].id` 确定性派生：先将 ID 转为大写，再拼接前后缀。
`display_name` 只用于展示，不影响变量名。

| 站点 ID | 邮箱变量 | 密码变量 |
|---|---|---|
| `primary` | `SUB2API_PRIMARY_EMAIL` | `SUB2API_PRIMARY_PASSWORD` |
| `backup_cn` | `SUB2API_BACKUP_CN_EMAIL` | `SUB2API_BACKUP_CN_PASSWORD` |

PowerShell 示例：

```powershell
$env:SUB2API_PRIMARY_EMAIL = "primary-operator@example.invalid"
$env:SUB2API_PRIMARY_PASSWORD = "<secret-from-your-secret-manager>"
$env:SUB2API_BACKUP_CN_EMAIL = "backup-operator@example.invalid"
$env:SUB2API_BACKUP_CN_PASSWORD = "<secret-from-your-secret-manager>"
uv run python main.py run
```

`SUB2API_<ID>_EMAIL` 和 `SUB2API_<ID>_PASSWORD` 是多站模式支持的凭据机制。两项都必须
进入实际 Persona Worker 的进程环境；只把值写入 Compose `.env` 并不等于已注入容器，
还要在 Compose `environment` 或 override 中显式映射对应变量名。邮箱值会去除首尾空白，
密码则原样读取，因此密码变量中意外的空格也会参与登录。

**不得在 `sources`、WebUI、插件 settings 或 `plugins/_config.json` 中保存 `email`、
`password`、token 等秘密。** `sources` 中出现未声明的凭据字段会使配置校验失败。

## 通知群继承

`notify_group_ids` 始终是显式允许列表，不是任意群广播目标。某站最终通知群按以下规则
计算并去重：

- `inherit_notify_group_ids: true`：顶层全局列表与本站 `notify_group_ids` 合并；
- `inherit_notify_group_ids: false`：只使用本站 `notify_group_ids`；
- 两边都为空：该站没有通知目标，发生变化时不会提交变化快照，直到配置有效目标。

`/sub2api report` 还会校验命令所在群：当前群必须在所选站点的最终允许列表中；选择
`all` 时，图表只包含当前群有权接收的站点。

## 命令与站点选择器

插件命令仅开发者可用。选择器可以是站点 `id`、唯一的 `display_name` 或 `all`；
匹配显示名称时不区分大小写。显示名称可能重复，因此脚本和排障时应优先使用稳定 ID。

| 命令 | 说明 |
|---|---|
| `/sub2api status [id\|display_name\|all]` | 查看站点启停、凭据就绪、快照数量和最近错误 |
| `/sub2api poll [id\|display_name\|all]` | 立即轮询；省略选择器时处理全部启用站点 |
| `/sub2api subscriptions <id\|display_name>` | 查询一个站点的当前可售订阅；只有一个启用站点时可省略选择器 |
| `/sub2api rates <id\|display_name>` | 查询一个站点的当前分组倍率；只有一个启用站点时可省略选择器 |
| `/sub2api report [id\|display_name\|all]` | 在已授权通知群生成并发送 Playwright 多站运行图 |
| `/sub2api reset [id\|display_name\|all]` | 删除所选站点状态（兼容模式删除旧版顶层状态）；下一次轮询静默初始化 |

所有上述状态读取、联网查询、轮询、图表和重置命令都只能由 `run_on_persona` 指定的人格执行，并仍要求开发者权限；`subscriptions` / `rates` 还要求有效凭据和唯一启用站点。`status` 不带选择器或显式使用 `all` 时列出全部已配置站点（含停用项）；`poll`、`report` 不带选择器或使用 `all` 时只处理启用站点；`reset all` 会清理全部已配置站点（含停用项）的状态。
若多个站点使用相同 `display_name`，请改用 ID；包含空格的唯一显示名称也可直接作为完整选择器使用。

## Playwright 与 Chromium

插件声明 Playwright Python 依赖，但非 Docker 环境仍需安装 Chromium 浏览器运行时：

```bash
uv run python -m playwright install chromium
```

Linux 首次部署且缺少系统库时，可由有权限的部署流程执行：

```bash
uv run python -m playwright install --with-deps chromium
```

官方 Docker 镜像会在环境构建阶段准备 Chromium，无需在每次插件重载时重复下载。
受信任的 PluginLoader 生命周期也会尝试安装依赖与 Chromium，但“插件已加载”或已安装
Playwright Python 包不代表浏览器一定可启动；浏览器安装失败时插件仍可文字模式工作。

可视化是附加能力，文字通知才是权威结果：

- 自动变化通知每轮最多尝试渲染 12 张变化卡；同一事件的成功图片复用于该事件所有待通知群；
- 自动变化通知渲染失败、超时、缺少 Playwright/Chromium 或图片校验失败时，会自动降级为纯文字，轮询与通知 ACK 流程继续；
- `visual_report_enabled: false` 只关闭自动变化图片，不影响文字通知，也不禁用显式 `/sub2api report`；
- `/sub2api report` 使用已经持久化的快照/状态生成图，不会现场请求站点；它没有文字看板降级，生成失败时会提示检查 Chromium；
- 图片在插件本地 artifact 目录生成并受数量、时效和大小限制；渲染时禁用 JavaScript 与外部网络请求。

## 旧版配置与状态迁移

为避免升级后突然丢失监控，插件保留旧版单站兼容路径：当配置中**没有** `sources` 键
时，顶层 `base_url`、接口路径等字段会作为一个兼容站点读取，继续使用
`SUB2API_EMAIL` / `SUB2API_PASSWORD`，并继续读写旧版顶层快照和 ACK 状态。

迁移到多站模式时：

1. 先备份 `plugins/_config.json` 和各 Persona 的 `sub2api_monitor` PluginDataStore；
2. 若希望沿用可确定归属的旧状态，首次切换时让显式 `sources` 只包含旧站对应的一个稳定 ID（例如 `primary`），不要同时加入其他站点；
3. 将凭据改为该 ID 派生的 `SUB2API_PRIMARY_EMAIL` / `SUB2API_PRIMARY_PASSWORD`；
4. 保留顶层 `poll_seconds`、`notify_group_ids`、`run_on_persona` 和可视化设置，并检查通知群继承规则；
5. 重载插件或重启指定 Persona，先执行 `/sub2api status primary`，再执行一次轮询以完成确定性迁移判定；
6. 确认匹配集合已沿用旧状态、未匹配集合已静默建立基线后，再按需加入其他站点。

新格式状态存放在 `source_states.<id>`。第一次使用显式 `sources` 轮询时，插件只会做
一次确定性旧状态迁移判定：必须恰好有一个可用目标、该目标两项环境变量凭据齐全，且
`source_states.<id>` 尚无既有状态。随后按 `subscriptions` 与 `group_rates` **逐集合
独立比对**旧顶层来源指纹；只有旧指纹与该目标解析后的 endpoint、账号摘要和时区完全
匹配，才把匹配集合的 snapshot、旧轮询 timestamps/error 与相关逐群 ACK 迁入该 ID。
匹配集合沿用旧基线；未匹配或不存在的集合会在下一次成功请求时静默建立新基线，因此
不会补发历史内容。

多目标、目标凭据不全、目标已有新状态或指纹不匹配时，插件绝不猜测归属，也不会自动
迁移相应集合。无论迁移、跳过还是部分匹配，旧顶层配置与状态都保留；不要手工复制 ACK，
因为事件键和来源指纹有严格兼容规则。显式 `sources: []` 仍会禁用全部站点且不触发迁移。
多站模式仍会写入顶层的聚合轮询时间/错误字段，每站事实状态位于
`source_states.<id>`。确认新状态稳定前请保留备份，避免同时在多个 Persona 运行旧、新
配置。

更改 `display_name` 只影响展示；更改站点 `id` 会改变凭据变量名和状态命名空间，等同于
新站迁移。来源指纹由站点 ID、解析后的监控端点、邮箱摘要和时区决定：更改账号、有效
监控端点或时区会静默建立新来源快照；仅轮换密码、改显示名，或只改页面路径但 origin
与最终端点未变时，不会重建基线。

## 变化、重试与隔离语义

- 每个站点分别认证、复用客户端并保存 `source_states.<id>`；一个站点失败不会阻止其他站点处理；
- 每站的订阅与倍率接口独立处理，一个接口失败时另一个仍可成功更新；
- 第一次快照及来源变化静默，不通知历史内容；符合上节确定性规则的旧集合则沿用迁移基线；
- 每个集合一次检测到的明细变化总数超过 20 时，不再逐条通知，而是合并为一条新增/移除/更新计数摘要；
- 每轮轮询在全部站点、集合、事件与群之间合计最多执行 200 次物理投递；额度会按本轮所选站点及订阅/倍率集合预先均分，前序站点持续失败也不能耗尽后序站点的额度；
- 达到分配额度的未投递群保持未 ACK。每个变化按站点、事件和群保存 ACK，失败或未确认时保留已经确认的群；后续轮询只重试尚未确认的群，并从上次尝试群的下一项继续，避免固定排在前面的失败群长期阻塞后续群；
- 只有允许列表内所有目标都确认后才提交对应变化快照；框架确认可能只表示适配器/平台已受理或确认发送，不表示最终用户已经阅读；
- 每个 HTTP 响应正文上限为 4 MiB，每个订阅或倍率集合最多规范化 2000 条记录；超过任一上限时该接口失败并保留已有快照；
- HTTP 401 会尝试刷新或重新登录，并只重试原请求一次，不会无限认证循环；
- 响应中的 token、password、api_key 等字段会在持久化、错误、命令输出和可视化前脱敏。

## 安全与排障

| 现象 | 检查项 |
|---|---|
| 后台任务未启动 | `run_on_persona` 是否精确匹配唯一 Persona；是否至少有一个启用站点同时具备两项凭据和有效通知群；是否误配 `sources: []` |
| 状态显示等待环境变量 | 按站点 ID（不是 `display_name`）检查 `SUB2API_<ID>_EMAIL/PASSWORD`，并确认变量已进入 Persona Worker/容器进程 |
| 配置提示缺少或非法接口 | `subscriptions_path`、`group_rates_path` 必须逐站填写；示例占位路径不能直接用于真实部署 |
| URL 校验失败 | 生产使用 HTTPS；完整接口 URL 必须与 `base_url` 同源、位于 API 根路径下，且不能含 userinfo、fragment、控制字符或秘密查询参数 |
| 某站变化一直不提交 | 检查该站最终通知群是否为空，以及群投递是否返回失败/未确认；已确认群不会重复发送 |
| `/sub2api report` 失败 | 安装 Chromium，检查 Persona 对 artifact 目录的写权限，并确认命令所在群属于所选站点允许列表 |
| 修改配置后再次静默初始化 | 检查是否更改了 ID、账号、站点、API/监控路径或时区；这些变化会建立新的来源基线 |
| 多站只想临时全部停用 | 显式设置 `"sources": []`；旧版顶层配置不会被回退启用，状态仍保留以供后续恢复或迁移 |

生产环境要求 HTTPS；仅本机调试可显式开启 `allow_insecure_http`。插件不会跟随 HTTP
重定向，也不会允许配置把认证请求或 Bearer Token 导向其他 origin。不要把真实站点、
接口、账号、密码或 token 提交到 README、示例文件或 Git 历史。

## 测试

测试使用假客户端、临时 artifact 和 Mock Transport，不访问真实站点：

```bash
uv run pytest plugins/tests/test_sub2api_monitor.py plugins/tests/test_sub2api_monitor_multisite.py -q
```
