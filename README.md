# health collar

## 2026-07-26 更新：按需实时心率测量

之前的链路都是"手表定时上报、服务器被动收"。这次加了一条反方向的：**AI 侧可以主动调用一个 MCP 工具，手表当场开一段 30 秒的 workout session，测量当下的实时心率并送回来**。全程佩戴者只需要点开Iwatch上的app——app 打开后会自动拉起15s的通知进程，测完手腕轻震一下。

```text
MCP tool (measure_heart_rate)
        ↓ 写入 command.json（单槽指令，30 分钟过期）
你的 ingest endpoint（要新挂两个口，见下）
        ↑ GET  /command          CollarWatch 轮询领指令
        ↑ POST /command/result   测完回执统计值
CollarWatch：HKWorkoutSession 30s 高频采样 → avg/min/max/样本数
```

**ingest 侧要挂的两个新口**（数据层函数已在 `health_store.py`，HTTP 壳照旧自备）：

- `GET /command` → 执行体 `fetch_pending_command()`。有指令返回 `{"command": "measure_heart_rate", "command_id": "...", "duration_seconds": 30, ...}`，没有返回 `{"command": null}`
- `POST /command/result` ← 手表送来 `{"command_id": "...", "result": {"heart_rate_average": 83, ...}}`，执行体 `complete_command()`

**手表侧新增**：`CommandFetcher.swift`（领令 / 回执 / 本地防重测记账）、`WorkoutMeasurer.swift`（测量本体，`discardWorkout` 收尾——训练记录不落库、健身三环不受污染，心率样本照常入 HealthKit 由原链路上报）；`Scheduler` / `CollarWatchApp` 接线（前台每 15s 轻轮询 + 测量中界面）。装机后健康授权会多弹一次 workout 写权限，允许即可。

**通知**：因每个人使用的推送方式不同，这里MCP 工具只下指令，没有发送通知的功能（指令下发后等最多 90 秒，没等到返回 pending，结果落 `health_now`）。app 开着会自己拉起进程；如果需要接收通知的话可以在你的 ingest 侧对接自己的推送渠道。

可调参数（环境变量）：`HEALTH_MEASURE_DURATION_S`（测量秒数，默认 30）、`HEALTH_COMMAND_TTL_MIN`（指令有效期，默认 30 分钟）。

> ⚠️ **一个值得单独说的坑**：HealthKit 会在**运行时审查权限描述文案的质量**。`NSHealthUpdateUsageDescription` 写占位敷衍话（比如本项目旧版的"仅测试环境写入模拟数据。"）,请求写权限时会直接抛 `NSInvalidArgumentException` 闪退，报错原话是 `The string "..." is an invalid value for NSHealthUpdateUsageDescription`。这次加了 workout 写权限，并对此进行了相关修正。
---

`health collar` 是一个面向个人使用的 Apple Health / Apple Watch 数据同步小工具。

它的目标很单纯：让 Apple Watch 上已经写入 HealthKit 的数据，以尽可能短而稳定的延迟同步到你自己的服务器，整理成普通文件，并在需要时通过 MCP 提供给 AI agent 使用（提供summary status和details 两种查看方式）。

项目不依赖数据库。服务端的数据层由标准库 Python 实现；Watch 端负责采集与上报；MCP 端只读取已经落盘的数据。

> 这不是医疗设备，也不对健康数据作诊断。它更适合作为个人记录、自动化和 AI 上下文的数据入口。

## 为什么不把 Health Auto Export 作为主链路

[Health Auto Export](https://www.healthexport.app/) 本身是很完整的 Health 数据导出工具，这个项目并不是为了替代它。两者解决的问题不同。

我最初也使用 HAE，但这个项目更在意“刚产生的数据多久能到自己的服务器”。在实际使用中，HAE 的自动化受 iOS 后台调度和 Health 数据访问条件影响，延迟并不总是适合近实时用途；其官方文档也说明，自动化只能在 iPhone 可访问健康数据的条件下运行，设备锁定、低电量模式以及系统后台资源都会影响执行时机。

因此，`health collar` 把主链路放到了 Apple Watch：

```text
Apple Watch / HealthKit
        ↓
CollarWatch
        ↓ HTTPS POST
你的 ingest endpoint
        ↓
health_store.py
        ↓
普通 JSON / JSONL 文件
        ↓
MCP / 其他读取端
```

HAE 仍然可以作为兼容输入源、迁移工具或没有 Watch 采集端时的备用方案。服务端保留了对 HAE REST payload 的解析支持。

## 实际同步表现

当前 Watch 端把下一次后台刷新预约在约 15 分钟之后，并在表盘上提供 complication。Apple 对 watchOS 后台刷新采用预算制调度：有活跃 complication 的 app 通常能获得更高的后台刷新预算，但系统只保证“不早于 preferred date”，并不保证精确按分钟唤醒。

在目前的实际使用中的表现是：

- **可以实现较为稳定的数据自动上传，正常使用中后台约 10–20 分钟出现一轮新上报，包括睡眠期间。** 这是实测范围，不是实时性 SLA；watchOS 可能因为电量、系统负载或后台预算延后任务。（非常建议把collar作为小组件添加到表盘上，可以使自动上传更加稳定）- **前台恢复时会立即补一轮。** Collar 进入 `.active` 状态后会直接执行一次采集和上传，所以打开 app、点 complication 或点“立即上报”可以主动催促刷新。
- **抬腕作为轻量催促。** 当系统抬腕后恢复到 Collar app 时，scene 会重新 active，从而触发上报；如果collar app被切走或被别的app顶掉则不行，这时点一下 点击打开collar也会直接触发上传。
- **断网不会推进 HealthKit anchor。** 数值类样本的 anchor 只在服务器返回 2xx 后提交；失败后下一轮仍会从旧 anchor 重新读取，因此可以依靠服务端幂等去重重传。

Apple 关于 watchOS 后台任务的说明：

- <https://developer.apple.com/documentation/watchkit/using-background-tasks>
- <https://developer.apple.com/documentation/watchkit/wkapplicationrefreshbackgroundtask>

## 数据在后台会怎么整理

服务端不是把每次 POST 原封不动堆起来。`server/health_store.py` 会先把 Watch 自定义 payload 和 HAE REST payload 归一成同一种样本结构，再做落盘和汇总。

默认文件：

```text
data/health/
├── latest.json                 # 每种指标当前最新值
├── samples-YYYYMMDD.jsonl      # 原始样本，按本地日拆分
├── wrist_baseline.json         # 腕温基线状态
└── sleep_history.jsonl         # 睡眠按天汇总，保留约 30 天
```

当前整理逻辑包括：

- 按指标和时间做幂等去重；
- 保存每种指标的最新样本；
- 对步数、距离、活动能量、运动时间等累计指标生成“今日总量”；
- 将最近睡眠阶段聚合成 core / deep / REM / awake 与时间轴；
- 计算睡眠期间的心率、HRV、呼吸等摘要；
- 保存最近睡眠历史，供 MCP 查询；
- 对返回给 agent 的长采样窗口做抽样，避免一次塞入过多上下文。

原始样本查询窗口按 48 小时设计；当前文件清理按“本地日文件”执行，因此磁盘上的最老文件在边界情况下可能略超过严格的 48 小时。MCP 的详细指标查询仍限制在最近原始窗口内。

## 当前支持的数据

### Watch 端目前直接采集

| 指标 | 内部名称 | 单位 |
|---|---|---|
| 心率 | `heart_rate` | count/min |
| HRV (SDNN) | `heart_rate_variability` | ms |
| 静息心率 | `resting_heart_rate` | count/min |
| 呼吸频率 | `respiratory_rate` | count/min |
| 睡眠腕温 | `apple_sleeping_wrist_temperature` | °C |
| 睡眠阶段 | `sleep_analysis` | 聚合结构 |
| 步数 | `step_count` | count |
| 步行/跑步距离 | `walking_running_distance` | mi |
| 爬楼层数 | `flights_climbed` | count |
| 活动能量 | `active_energy_burned` | kcal |
| 运动时间 | `apple_exercise_time` | min |

可以根据需求另外添加数据种类——具体能不能得到某项数据仍取决于 Apple Watch 型号、地区、系统版本、HealthKit 权限以及设备是否实际产生了这类样本。

### 服务端额外兼容

服务端 allow-list 里还包含：

- `blood_oxygen_saturation`

但**当前 `watch/` 采集端并没有请求或查询血氧类型**。如果使用 HAE 或其他采集端上报血氧，服务端可以接收；如果希望 CollarWatch 本身采集，需要另外在 `HealthCollector.swift` 中加入对应 HealthKit 类型，并考虑设备/地区可用性。

可以通过 `HEALTH_ALLOWED_TYPES` 覆盖服务端允许的指标集合。

## Watch 端安装

需要：

- Xcode
- [XcodeGen](https://github.com/yonaskolb/XcodeGen)
- watchOS 10 或更高版本
- 一台用于真机安装的 Apple Watch

### 1. 配置 endpoint 和 token

复制本地 token 模板：

```bash
cp watch/Sources/Config.local.swift.example watch/Sources/Config.local.swift
```

填写 token：

```swift
enum ConfigLocal {
    static let token = "YOUR-LONG-RANDOM-TOKEN"
}
```

然后修改 `watch/Sources/Config.swift`：

```swift
static let endpoint = URL(string: "https://your-server.example.com/api/health")!
```

建议只使用 HTTPS。健康数据和 ingest token 都不应该通过明文 HTTP 发送。

### 2. 配置签名

修改 `watch/project.yml`：

- `DEVELOPMENT_TEAM`: 你的 Apple Team ID；
- `PRODUCT_BUNDLE_IDENTIFIER`: 换成你自己的唯一 Bundle ID；
- widget target 的 Bundle ID 一并修改。

### 3. 生成工程并安装

```bash
cd watch
xcodegen generate
open CollarWatch.xcodeproj
```

在 Xcode 中选择你的 Apple Watch 并运行。

首次启动后，在 Watch 上完成 HealthKit 授权。把 Collar complication 放到当前使用的表盘上，有助于获得更稳定的后台刷新预算。

使用免费个人签名安装时，开发构建通常需要定期重新签名；使用 Apple Developer Program 的正式开发签名则不受 7 天个人签名周期限制。

## 服务端接入

`server/health_store.py` 是**数据层**，不是完整的 Web 服务。仓库没有替你决定 FastAPI、Flask、反向代理、域名或鉴权方式。

你需要在自己的 HTTPS ingest endpoint 中完成两件事：

1. 校验 `X-Health-Token`；
2. 把 JSON body 交给 `normalize_payload()` / `store_samples()`。

示意：

```python
from health_store import ALLOWED_TYPES, normalize_payload, store_samples

# 先在你的 Web 层校验 X-Health-Token。
# 验证通过后：
samples = normalize_payload(body)
samples = [s for s in samples if s["type"] in ALLOWED_TYPES]
stored, deduped = store_samples(samples)
```

**注意：当前仓库本身没有 HTTP ingest server，也不会自动读取请求头。** `.env.example` 中的 `HEALTH_INGEST_TOKEN` 只是给你自己的入口层使用的约定。不要把未鉴权的写入接口直接暴露到公网。

## MCP

MCP server 需要 Python 3.10 或更高版本：

```bash
pip install -r requirements.txt
```

配置示例：

```json
{
  "mcpServers": {
    "health-collar": {
      "command": "python",
      "args": ["/absolute/path/to/health-collar/mcp_server/server.py"],
      "env": {
        "HEALTH_DATA_DIR": "/absolute/path/to/health-collar/data/health",
        "HEALTH_TZ_OFFSET_HOURS": "8"
      }
    }
  }
}
```

提供两个工具：

### `health_now`

给 agent 的紧凑快照，适合先看“现在大概是什么状态”。目前会整理：

- 最新心率、静息心率、HRV、呼吸；
- 最近一晚睡眠及阶段；
- 睡眠期间的心率 / HRV / 呼吸摘要；
- 腕温；
- 今日步数、距离、活动能量、运动时间、爬楼层数。

### `health_detail`

用于继续向下查：

- 心率 / HRV / 呼吸：最近最多 2 小时的逐点样本和 min / max / avg；
- 睡眠：指定日期的睡眠阶段时间轴、睡眠期 vitals 与最近 7 天平均睡眠时长。

把 MCP 接给任何 agent，都意味着那个 agent 在调用工具时能够读取这些健康数据。请按你自己的信任边界配置 MCP 和服务器权限。

## 配置项

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `HEALTH_DATA_DIR` | `./data/health` | 健康数据文件目录 |
| `HEALTH_TZ_OFFSET_HOURS` | `0` | 固定 UTC 偏移，用于“今天”和展示时间 |
| `HEALTH_ALLOWED_TYPES` | 内置列表 | 逗号分隔的指标 allow-list |
| `HEALTH_INGEST_TOKEN` | 无 | 约定给你自己的 HTTP ingest 层使用；核心数据层不会自动校验 |

`HEALTH_TZ_OFFSET_HOURS` 是固定偏移，不是 IANA timezone。处于夏令时地区时需要自行调整，否则跨 DST 时“今天”和睡眠日期的边界可能偏一小时。

## 已知限制

### watchOS 调度不是定时器

15 分钟是请求系统“不要早于这个时间唤醒”，不是强制定时。活跃 complication 能提高后台预算，但 watchOS 仍可能延后或跳过某次后台刷新。

### Watch 必须有可用网络

上传需要 Apple Watch 当时能够联网，例如：

- 直接 Wi-Fi；
- 蜂窝网络；
- 或通过与 iPhone 的连接获得网络能力。

没有网络时，本轮上传会失败；因为 HealthKit anchor 不会在失败时提交，后续成功运行时会重新读取未确认的数据。

### “抬腕催促”取决于 app 是否重新 active

代码没有使用私有 API 监听抬腕动作。它监听的是 SwiftUI `scenePhase == .active`。所以最稳定的主动刷新方式仍然是打开 Collar、点表盘 complication 或使用“立即上报”。

### Watch 采集并不等于完整 Apple Health

当前采集端只读代码里列出的 HealthKit 类型。iPhone 上由其他设备或 app 产生、但没有同步/暴露给 Watch 的数据不会自动出现；没有佩戴手表的时段也可能产生缺口。

### 累计指标是原始样本求和

目前步数、距离、活动能量等“今日总量”由服务器对收到的样本求和。对于单一 Watch 数据源这很直接；如果同时混入多个会产生重叠区间的 HealthKit 来源，仍可能出现重复累计。不要同时让多个采集链路长期上报同一批累计指标，除非你已经明确处理了来源优先级。

### 原始数据默认不加密

服务器落盘的是普通 JSON / JSONL。磁盘加密、备份策略、HTTPS、反向代理和访问控制都由部署环境负责。

## HAE 兼容模式

如果暂时不装 Watch app，也可以继续让 Health Auto Export POST 到同一个 ingest endpoint。`normalize_payload()` 能识别其 REST metrics 格式。

需要注意：HAE 与 Watch 同时长期上报相同的累计指标可能造成重复计数。迁移时更稳妥的做法是选定一个主采集源，再保留另一个作为临时回填或备用。

HAE 关于自动化限制的官方说明：

- <https://help.healthyapps.dev/en/health-auto-export/automations/>
- <https://help.healthyapps.dev/en/health-auto-export/troubleshooting/>

## 项目结构

```text
health-collar/
├── server/
│   └── health_store.py          # 归一化、落盘、汇总、查询
├── mcp_server/
│   └── server.py                # health_now / health_detail
├── watch/
│   ├── Sources/                 # Watch app
│   ├── Widget/                  # 表盘 complication
│   └── project.yml              # XcodeGen 配置
├── .env.example
├── requirements.txt
└── LICENSE
```

## License

MIT. See [LICENSE](LICENSE).
