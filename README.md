# KiraAI Token 用量统计（Token Stats）

> **v1.2.7**：价格规则 WebUI 可视化编辑 + 余额类型配置说明优化 + 自报统计排除

为 KiraAI 提供完整的 **Token 用量统计看板**：逐轮记录输入/输出/缓存 tokens、按价格规则实时估算费用（峰谷价、双币种）、API 余额监测（探测 + 估算 + 对表校准）、出错统计——WebUI 侧边栏仪表盘 + 悬浮挂件 + bot 工具（概览/聚合/明细）+ 可选自定义命令，四大入口全覆盖。

> 移植自 [Alife 的 TokenStats](https://github.com/1chuxin/1chuxin-Alife.TokenStats)（初心出品，参考其设计），并整合了 [KiraAI-plugin-api-balance](https://github.com/ChuXia2004/KiraAI-plugin-api-balance) 的查询模式与命令设计。
> **模型无关**：任何 Provider 只要在 LLMResponse 里上报 tokens 就能统计。

---

## ✨ 功能一览

| 功能 | 说明 |
|------|------|
| 📊 **逐轮采集** | 每轮 LLM 调用（含工具中间步）的 输入/输出/缓存 tokens，JSONL 落盘，重启不丢；**热读缓存**（mtime+length 判失效），大日志下查询/轮询不重复全量读盘 |
| 💰 **费用估算** | 价格规则按 `URL > 模型 > 渠道名` 加权匹配（4/2/1 分），峰谷价（工作日 9-12 点、14-18 点为峰）；展示时实时计算，**改价后全历史即时重定价**；**双币种**（CNY 元 / 积分）分桶累计永不混算 |
| 💳 **余额监测** | 六类监测源：`auto`（按 URL 自动分流官方端点/One-API 中转站）、`custom`（自定义接口多端点尝试 + json_path）、`newapi`（New-API 站点：New-Api-User 头 + quota 换算）、`preset`（预设扣减钱包型）、`daily`（每日重置积分）、`rolling`（每日累计滚存积分）；估算型支持**「当前余额(对表)」**：填上游后台真实余额即校准，此后按价格规则自动扣减 |
| 🔍 **来源归类** | 自定义关键词规则优先 → 群聊/私聊自动判定 → 工具续轮继承上一轮，可自定义标签名 |
| 🖥️ **WebUI 仪表盘** | 侧边栏「Token 用量」：概览卡片（KPI + 迷你走势线 + 双币种费用）、**时间趋势**（按天柱状 + Top8 模型费用分色堆叠 + 缓存命中率虚线，点柱下钻单天按小时 → 点小时看记录）、按天历史、今日按小时、维度分析、**会话统计**（细到每个会话如 `qq:dm:12345`，按私聊/群聊归类汇总，点击看该会话逐轮明细）、最近逐轮记录、价格规则、余额监测 |
| 🤖 **Bot 工具** | 三个工具：`query_token_stats`（概览）、`query_token_usage`（维度聚合：channel/model/source/day + 过滤 + top）、`query_token_records`（逐轮明细 + minInput 查大上下文），输出带 4000 字符硬上限防回注爆上下文 |
| ⌨️ **自定义命令** | 可选 `/用量` 等命令词直接查询（默认关闭，防打扰），支持用户白名单 |
| 🪟 **悬浮挂件** | 默认关闭。侧边栏「Token 挂件」页：迷你卡片实时显示会话/今日 tokens、费用、余额，可拖动（位置记忆）、可折叠成小球、紧凑模式，**可弹出独立小窗**（⧉ 按钮，浏览器允许弹窗后即成为真正可拖动的独立挂件窗口），适合浏览器小窗钉角落或 OBS 采集 |
| ⚠️ **出错统计** | 扫描 AI 输出中的「出错：」标记（errScanPos 游标，工具循环不重复计数），按范围聚合展示 |

---

## 📦 安装

### 方法一：插件市场（若已上架）

1. 打开 KiraAI WebUI → **插件管理**
2. 搜索 `KiraAI_token_stats_plugin` → 安装
3. 在插件列表里启用

### 方法二：GitHub 安装

1. 打开 KiraAI WebUI → **插件管理** → **安装插件**
2. 选择 **从 GitHub 安装**，填入：
   ```
   https://github.com/znq19/KiraAI_token_stats_plugin
   ```
3. 安装完成后在列表里启用
4. 若提示缺少 `aiohttp`（余额监测需要），在插件管理里点 **安装依赖**（自动读取仓库内 `requirements.txt`），或手动执行：
   ```bash
   pip install aiohttp
   ```

### 方法三：手动安装

1. 下载 ZIP：`https://github.com/znq19/KiraAI_token_stats_plugin/archive/refs/heads/main.zip`
2. 解压后把整个目录 `KiraAI_token_stats_plugin` 复制到 KiraAI 的插件目录：
   ```
   data/plugins/KiraAI_token_stats_plugin/
   ```
3. 重启 KiraAI（或热重载插件）

目录内需包含：`main.py`、`manifest.json`、`schema.json`、`requirements.txt`、`icon.svg`、`icon-dark.svg`。

---

## 🚀 快速上手

### 1. 启用统计（默认已启用）

安装后插件即开始记录。发几条消息后打开 **侧边栏 → Token 用量** 即可看到数据。

### 2. 配置价格规则（可选，否则费用显示「—」）

打开 **侧边栏 → Token 用量 → 价格规则** 页，点「＋ 添加规则」即可可视化编辑（名称/URL/模型/渠道匹配、币种、峰谷开关、六个价格字段），支持编辑/删除，保存后全历史费用即时重算。无需手写 JSON。

预置了 DeepSeek V4-Flash / V4-Pro 官方峰谷价。自定义规则示例：

```json
[
  {
    "name": "我的中转站",
    "url_match": "myproxy.example.com",
    "currency": "CNY",
    "peak_enabled": true,
    "hit_peak": 0.1,
    "hit_off": 0.05,
    "miss_peak": 3.0,
    "miss_off": 1.5,
    "out_peak": 9.0,
    "out_off": 4.5
  },
  {
    "name": "京东积分渠道",
    "url_match": "jd-llm.example.com",
    "currency": "积分",
    "peak_enabled": false,
    "hit_peak": 0.5,
    "hit_off": 0.5,
    "miss_peak": 10.0,
    "miss_off": 10.0,
    "out_peak": 30.0,
    "out_off": 30.0
  }
]
```

字段说明（价格单位：**元 或 积分 / 百万 tokens**）：

| 字段 | 说明 |
|------|------|
| `name` | 规则名称（展示用） |
| `url_match` | 匹配 endpoint 域名（**推荐**，比渠道名稳定，双向包含匹配） |
| `model_match` | 匹配模型名（如 `flash`、`pro`） |
| `channel_match` | 匹配渠道名/主机名 |
| `currency` | 计价币种：`CNY`=元/百万tokens（默认），`积分`=积分/百万tokens（京东等积分制渠道）。费用按币种分开累计，不与 ¥ 混算 |
| `peak_enabled` | 是否启用峰谷价（false = 恒按谷价） |
| `hit_peak` / `hit_off` | 缓存命中部分的单价（峰/谷） |
| `miss_peak` / `miss_off` | 未命中缓存部分的单价（峰/谷） |
| `out_peak` / `out_off` | 输出 tokens 单价（峰/谷） |

> 匹配规则取**加权分最高**的一条：URL 命中 +4 分、模型命中 +2 分、渠道名命中 +1 分（可叠加）。建议用 `url_match` 按域名配置——渠道重排/改名不受影响；同一域名下不同模型（如 flash/pro）用 `url_match + model_match` 组合精确区分。

### 3. 配置余额监测（默认已启用）

打开 **侧边栏 → Token 用量 → 余额监测** 页，点「＋ 添加监测源」按类型填表即可（支持编辑/删除/启用开关），无需手写 JSON。类型怎么选：

| 类型 | 什么时候用 | 要填什么 |
|------|-----------|---------|
| `auto` | 官方平台（DeepSeek/Kimi/硅基/智谱）或 One-API 系中转站，**最省事** | 站点地址 + API Key |
| `custom` | 有自定义余额接口 | 接口地址（可选 json_path 指定余额字段） |
| `newapi` | New-API 站点 | 站点地址 + 系统访问令牌 + 纯数字用户ID（+ 换算比例可选） |
| `preset` | 固定钱包型：额度用一点少一点、不会自动恢复 | 「当前余额(对表)」= 现在后台看到的真实余额，之后每次调用按价格规则自动扣减 |
| `daily` | 每日重置积分：每天固定发 N 积分，当天用完第二天重置 | 「每日额度」+「刷新时刻 HH:mm」 |
| `rolling` | 每日累计滚存积分：每天发 N 积分，用不完的累积到下一天 | 「每日额度」+「刷新时刻」+「当前余额(对表)」= 现在后台看到的真实余额作为基准 |

> 💡 拿不准就选 `auto` 填地址+Key；`preset/daily/rolling` 是给没有余额接口的渠道用的，靠价格规则推算，记得先配好价格规则。

**方式一：官方平台快捷分区**（DeepSeek / Kimi / 硅基流动 / 智谱）——填 API Key 即自动并入，无需写 JSON。

**方式二：New-API 中转站简易文本格式**（对齐 [api-balance 插件](https://github.com/ChuXia2004/KiraAI-plugin-api-balance)）——配置页 → **New API 站点（简易文本格式）**，每行一个，英文分号分隔：

```
我的站点1;https://api.example.com;sk-xxxxxxxx;123456;500000
```

字段依次为：`名称;base_url;系统访问令牌;纯数字用户ID;换算比例(可选，默认500000)`。自动并入余额监测（type=newapi），quota ÷ 换算比例 = 元。

**方式三：高级 JSON**（`余额监测源`，支持全部六类）：

```json
[
  {
    "name": "DeepSeek 官方",
    "type": "auto",
    "url": "https://api.deepseek.com",
    "api_key": "sk-xxxxx",
    "enabled": true
  },
  {
    "name": "我的中转站",
    "type": "custom",
    "url": "https://myproxy.example.com",
    "api_key": "sk-yyyy",
    "json_path": "",
    "enabled": true
  },
  {
    "name": "小鸡 NewAPI",
    "type": "newapi",
    "url": "https://newapi.example.com",
    "api_key": "sk-zzzz",
    "api_user": "123456",
    "quota_conversion": 500000,
    "enabled": true
  },
  {
    "name": "钱包备用金",
    "type": "preset",
    "url": "",
    "anchor_balance": 45.5,
    "anchor_at": "2026-08-31T10:00:00",
    "currency": "CNY",
    "enabled": true
  },
  {
    "name": "京东每日积分",
    "type": "daily",
    "url": "jd-llm.example.com",
    "daily_quota": 1000,
    "refresh_time": "00:00",
    "anchor_balance": 800,
    "anchor_at": "2026-08-31T09:00:00",
    "currency": "积分",
    "enabled": true
  },
  {
    "name": "积分滚存",
    "type": "rolling",
    "url": "rolling-llm.example.com",
    "daily_quota": 100,
    "refresh_time": "00:00",
    "anchor_balance": 350,
    "anchor_at": "2026-08-31T00:00:00",
    "currency": "积分",
    "enabled": false
  }
]
```

| 类型 | 行为 |
|------|------|
| `auto` | 按 URL 自动分流：DeepSeek → `/user/balance`；Moonshot/Kimi → `/v1/users/me/balance`；硅基流动 → `/v1/user/info`；智谱 → `/api/paas/v4/users/me/balance`；其他一律按 One-API/New-API 中转站探测（subscription − usage） |
| `custom` | 依次尝试常见余额接口（`/user/balance`、`/v1/users/me/balance`、`/v1/user/info`、`/api/paas/v4/users/me/balance`、One-API 组合）；接口特殊可填**完整余额接口 URL** + `json_path` 取数（如 `data.available_balance`） |
| `newapi` | **New-API 站点专属**：请求带 `New-Api-User` 头（站点后台纯数字用户ID），打 `/api/user/self`，自动从 quota/balance/remaining/points 等字段提取额度，按 `quota_conversion` 换算（默认 500000，即 quota ÷ 500000 = 额度单位） |
| `preset` | 预设扣减（钱包型）：填 `initial`（初始额度）→ 当前 = 初始额度 − 该渠道累计计费；或填 `anchor_balance`（当前余额对表）→ 当前 = 设定值 − 其后计费（改价即时重估） |
| `daily` | **每日重置积分**：填 `daily_quota`（每日额度）+ `refresh_time`（刷新时刻，默认00:00）→ 当前 = 每日额度 − 上次刷新以来计费；可填 `anchor_balance` 在本周期内校准（跨刷新自动回落每日额度模型） |
| `rolling` | **每日累计滚存积分**：填 `anchor_balance` 建立基准 + `daily_quota` → 当前 = 设定余额 − 累计计费 + 发放次数 × 每日额度（没用完的结转滚存） |

估算型（preset/daily/rolling）核心字段：

| 字段 | 说明 |
|------|------|
| `anchor_balance` | **当前余额(对表)**：在后台看到的真实余额。填写即设定基准（填写时刻 = 对表点），此后 = 设定值 − 其后该渠道计费（按价格规则估算，改价自洽）。上游对不上时改一次数字即完成校准，此前的一切估算误差（价格规则错、漏记、多客户端、上游手动消耗）被吸收 |
| `anchor_at` | 对表时刻（ISO 格式 `2026-08-31T10:00:00`）；填写 `anchor_balance` 时也自动取当前时间，一般无需手填 |
| `daily_quota` | 每日额度（daily/rolling 的每周期发放额度） |
| `refresh_time` | 每日刷新时刻（HH:mm，默认 00:00）。纯时间推导不落状态，客户端离线期间的发放不丢 |
| `currency` | 展示币种；daily/rolling 默认「积分」，其他默认 CNY |

> 💡 余额单位：插件配置页 → 余额监测 → `balance_unit`（默认"元"），可改为"额度"、"美元"、"美刀"、"点数"等任意单位；积分制源自动显示「积分」不受影响。

> ⚠️ **安全提示**：`api_key` 以**明文**存储在插件配置文件（`data/config/plugins/KiraAI_token_stats_plugin.json`）中，请确保服务器文件权限安全。轮询间隔默认 60 分钟（最小 5 分钟）。

### 4. 让 bot 回答用量

启用 **Bot 工具**（默认开启）后，用户直接问：

> “今天用了多少 token？”
> “花了多少钱？”
> “余额还剩多少？”
> “哪个模型用得多？某渠道花了多少？昨天按小时的用量？”

bot 会自动调用对应工具返回结果。三个工具：

| 工具 | 用途 |
|------|------|
| `query_token_stats` | 概览：本次/今天/7天/30天/累计 + 余额 |
| `query_token_usage` | 维度聚合：`dim=channel/model/source/day`，支持 `range/from/to` 时间区间、`model/channel/source` 关键字过滤、`top` 行数上限（默认8，最大20） |
| `query_token_records` | 最近 N 轮逐轮明细（倒序），支持过滤 + `minInput`（只看输入超过某 token 数的轮次，定位大上下文） |

### 5. 自定义命令（可选，默认关闭）

插件配置页 → **自定义命令**：

1. `enable_command` 打开
2. `command_words` 添命令词（默认 `/用量`、`/token`，支持前缀匹配：`/用量 今天`）
3. 可选填写 `allowed_users` 白名单（留空 = 所有人可用）
4. 命令参数：`本次 / 今天 / 7天 / 30天 / 累计 / 余额`（留空 = 全部概览）

---

## 🖥️ WebUI 仪表盘

侧边栏 → **Token 用量**：

- **概览**：快照栏（来源/会话轮数/进行时长/最近一轮）+ 五个范围卡片（本次/今天/近7天/近30天/累计：KPI 数字 + 迷你走势线、轮数、输入、输出、缓存、命中率、双币种费用、出错）+ 按天历史（今日高亮 + 条形分布）+ 今日按小时柱状图（点击小时 → 查看该小时记录）
- **时间趋势**：按天柱状图（柱高=总量），Top8 模型费用分色堆叠，未计价灰色兜底；紫色虚线标注缓存命中率；**点日柱 → 下钻该天按小时 → 点小时柱 → 查看该小时前后记录**，「← 返回按天」回退
- **维度分析**：按 来源 / 渠道 / 模型 / 会话 四个维度聚合（可切 今天/7天/30天/累计），费用双币种分列
- **最近记录**：最近 15 轮逐轮明细（时间/模型/来源/渠道/各 tokens/双币种费用）
- **价格规则**：当前规则展示（含币种列），可「＋ 添加规则」/编辑/删除，保存后全历史费用即时重算
- **余额监测**：各源余额/类型/更新时间/状态（估算型标注「估算」），可手动「立即探测」，可「＋ 添加监测源」/编辑/删除
- **Token 挂件**（需在配置页「挂件」区开启）：迷你悬浮卡片——会话/今日 tokens、今日费用（双币种）、输入/输出、当前模型、前 3 个余额源；标题栏可拖动（位置 localStorage 记忆）、折叠按钮收成小球、紧凑模式，10 秒自动刷新；**⧉ 按钮弹出独立小窗**（`window.open` popup，浏览器允许弹窗后即成为真正可拖动的独立挂件窗口，自带关闭按钮，卡片填满窗口）；「完整看板」链接直达全量页。适合浏览器开小窗钉角落，或 OBS 采集当直播挂件

数据每 5 秒自动刷新。

---

## 🛠️ 数据与文件

| 路径 | 说明 |
|------|------|
| `data/plugin_data/KiraAI_token_stats_plugin/usage-log.jsonl` | 用量日志（每轮一行） |
| `data/plugin_data/KiraAI_token_stats_plugin/balance_state.json` | 余额探测状态缓存 |
| `data/config/plugins/KiraAI_token_stats_plugin.json` | 插件配置（含价格规则、余额源 api_key） |

日志行格式：

```json
{"t":"2026-08-30T23:45:01.123","v":1234,"i":1000,"o":234,"c":500,"m":"deepseek-v4-flash","s":"gm","ch":"api.deepseek.com","h":"api.deepseek.com","sid":"qq:gm:12345"}
```

| 字段 | 含义 |
|------|------|
| `t` | 时间戳 |
| `v` / `i` / `o` / `c` | 总量 / 输入 / 输出 / 缓存命中 tokens |
| `m` | 模型名 |
| `s` | 来源（gm/dm/system/自定义） |
| `ch` | 渠道（endpoint 域名或 provider） |
| `h` | endpoint 域名 |
| `sid` | 会话 ID |
| `e` | （可选）该轮「出错：」次数 |

---

## ⚙️ 配置项总览

| 区块 | 配置 | 默认 | 说明 |
|------|------|------|------|
| 基础设置 | `enabled` | `true` | 总开关 |
| 基础设置 | `debug_log` | `false` | 调试日志 |
| 基础设置 | `source_rules` | `{}` | 自定义来源关键词规则 `{"关键词":"来源名"}` |
| 来源归类 | `source_default` / `source_group` / `source_dm` | `system` / `gm` / `dm` | 兜底/群聊/私聊来源标签 |
| 自定义命令 | `enable_command` | `false` | 命令开关 |
| 自定义命令 | `command_words` | `["/用量","/token"]` | 命令词列表 |
| 自定义命令 | `allowed_users` | `[]` | 白名单（空=全部） |
| 自定义命令 | `exact_match` | `false` | 价格规则 URL/模型名/渠道名全字匹配 |
| 自定义命令 | `denied_message` | 权限不足… | 无权限提示 |
| 自定义命令 | `command_success_template` | `📊 {provider}：{result}` | 单结果模板 |
| Bot 工具 | `enable_tool` | `true` | 工具开关（含三个工具） |
| Bot 工具 | `tool_include_balance` | `true` | 工具结果附带余额 |
| 价格规则 | `rules` | DeepSeek 官方价 | 价格规则数组（含 `currency` 币种字段） |
| 余额监测 | `enable_balance` | `false` | 余额监测开关 |
| 余额监测 | `balance_interval` | `60` | 轮询间隔分钟（≥5） |
| 余额监测 | `balance_sources` | `[]` | 监测源数组（auto/custom/newapi/preset/daily/rolling） |
| 余额监测 | `balance_unit` | `元` | 余额显示单位（可自定义：额度/美元/美刀等） |
| 高级设置 | `max_log_size` | `100000` | 日志保留条数（0=不裁剪） |
| 高级设置 | `session_idle_minutes` | `30` | 「本次会话」滚动窗口分钟数 |
| 高级设置 | `session_expire_minutes` | `30` | 会话临时状态（来源继承/错误游标）无活动清理分钟数 |

---

## ❓ FAQ

**Q：费用显示「—」？**
A：没有匹配到价格规则。查看 价格规则 页确认规则是否覆盖你的模型/渠道，按域名（`url_match`）配置最稳。积分制渠道记得在规则里把 `currency` 设为 `积分`。

**Q：为什么日志里没有我的模型名？**
A：模型名取自默认 LLM 客户端的 `model_id`/`model` 字段；如果你的 Provider 没暴露这些字段会显示「未知」。渠道识别同理，取 `base_url` 的域名，取不到显示「默认渠道」。

**Q：余额一直「尚未探测」？**
A：检查三点：① 余额监测开关已开启；② 源已 `enabled`；③ 轮询间隔到了（或用页面「立即探测」）。custom 源报错信息会给出具体原因（HTTP 状态/响应摘要）。preset/daily/rolling 估算型不需要接口，直接配置对表/额度字段即可。

**Q：「当前余额(对表)」有什么用？**
A：估算型余额源（preset/daily/rolling）不需要接口，成本全靠价格规则推算，误差会累积。直接在后台看一眼真实余额填到 `anchor_balance`，之后自动按「设定值 − 其后计费」推算，误差被吸收；上游对不上时改一次数字即重新校准。

**Q：改价格后历史费用会变吗？**
A：会。费用**只在展示时计算**（日志只存 tokens/模型/渠道/时间戳），改价即时全历史重定价，双币种同步。

**Q：能统计多开/多 bot 实例吗？**
A：当前为单实例统计（汇总 + 按会话 sid 维度分析）。多实例并发写日志有文件锁保护，不会写坏。

**Q：aiohttp 没装会怎样？**
A：统计/工具/页面全部正常，仅余额监测不可用（加载时日志有提示，插件管理里点「安装依赖」自动装）。

---

## 📄 许可证

[AGPL-3.0](LICENSE) — 修改后再分发需开源。

## 🙏 致谢

- [1chuxin/1chuxin-Alife.TokenStats](https://github.com/1chuxin/1chuxin-Alife.TokenStats) — 原版 Alife 插件（功能设计参考）
- [ChuXia2004/KiraAI-plugin-api-balance](https://github.com/ChuXia2004/KiraAI-plugin-api-balance) — 余额查询模式参考

---

## 📝 更新日志

<details>
<summary>点击展开</summary>

### v1.2.8（2026-08-31）

- **会话级统计**：新增「会话统计」页（WebUI 侧边栏 → Token 用量 → 会话统计），细到每个会话（如 `qq:dm:12345` / `qq:gm:12345`），展示轮数/输入/输出/缓存/总量/费用/最近活动；顶部按 **私聊 / 群聊 / 其他** 整体归类汇总卡片；点击某会话行直接跳转该会话的逐轮明细
- **查余额默认启用**：`enable_balance` 默认值改为 **开**（升级后余额监测自动生效，无需手动开启）；自定义命令（`/用量` 等）仍默认关闭
- **随机背景开关反馈**：点击右下角 👕 按钮切换时弹出「随机背景：开/关」提示，图标同步变化（👕 开 / 🚫 关），悬停 title 也显示当前状态
- **挂件弹窗修复**：挂件「独立小窗 ⧉」与「打开看板」按钮改为直接新标签页打开统计面板（原 `window.open('', '_blank')` 空窗写法被浏览器拦截导致无响应）

### v1.2.7（2026-08-31）

- **价格规则 WebUI 可视化编辑器**：侧边栏「Token 用量 → 价格规则」页新增「＋ 添加规则」——按表单填写（规则名称/URL/模型/渠道匹配、币种、峰谷开关、缓存命中/未命中/输出 峰谷六价），支持编辑/删除，保存后自动热重载并即时重算全历史费用，无需再手写 JSON
- **工具结果失败统计排除自报**：本插件自身工具（query_token_stats/usage/records）的正常输出文本含「失败/出错」字样（如"工具结果失败：N 次"、"最近出错：…"、"后台日志错误：…（最近：Merge facts error）"）时不再被误计为一次工具失败——新增 `_SELF_TOOL_RE` 前缀排除，避免 bot 查一次统计就自增一次错误
- **余额类型配置说明优化**：schema 与 README 用大白话解释 preset/daily/rolling 三种估算型怎么填（preset=固定钱包用一点少一点；daily=每日重置；rolling=每日累计滚存），并给出「拿不准就选 auto」的引导
- **配置说明引导侧边栏**：插件配置页顶部说明改为推荐在 WebUI 侧边栏「Token 用量」页查看数据与配置（价格规则/余额监测均可页面内可视化编辑），不再引导手写 JSON

### v1.2.6（2026-08-31）

- **后台日志 ERROR 扫描**：新增对 KiraAI `data/log.log`（含轮转文件）的增量扫描（10s 间隔），捕捉控制台/日志里的真实错误——重点：**LLM 输出错误 XML 格式导致解析失败**（`Error parsing message: mismatched tag` 等）。按天分类聚合近 7 天：XML解析 / 模型调用 / 工具执行 / 网络超时 / 异常堆栈 / 其他；工具/命令摘要与 WebUI 仪表盘新增「后台日志 ERROR」卡片。与原有「出错：」响应内扫描并存
- **轮转文件按 inode 跟踪游标**：`log.log.1/2/…` 全部纳入扫描（历史 ERROR 也能统计到）；轮转改名后同一文件（同 st_ino）继续从原游标扫，不重复计数；文件截断/重建（新 ino）自动从头扫。修复此前只扫 log.log、轮转瞬间切文件导致游标重置重复计数的问题
- **扫描游标持久化**：`err_stats.json` 同时保存各文件 ino 游标，热重载后新实例从原游标续扫——历史 ERROR 不重复计数（修复热重载一次统计翻倍一次的问题）
- **错误统计持久化**：日志错误与工具失败统计每 30s 节流落盘 `err_stats.json`，热重载/重启自动恢复；`terminate` 时**强制落盘**（force 参数绕过节流），不再因距上次保存不足 30s 而丢最近统计
- **二进制模式扫描**：日志文件以 `rb` 读取，`tell()/seek()` 返回真实字节偏移——Python 3.10-3.12 文本模式 `tell()` 返回不透明 cookie，与 `st_size` 比较会误判截断导致重复计数，3.13 起才返回真实偏移；二进制模式全版本兼容
- **工具结果失败统计**：新增 `on.tool_result` 钩子，捕捉工具返回的失败结果——**error 返回 / 权限 denied（Permission denied、拒绝访问、403 Forbidden、HTTP 403）/ 超时 / 调用失败 / 未实现**等，这些都属于 LLM 白烧 token 的典型场景。按天聚合近 7 天，工具/命令摘要与 WebUI 仪表盘新增「工具结果失败」卡片
- **失败判定防误报**：`{"error": 0}`（很多 API 的成功约定）不再误判为失败，error 字段仅当值为非零数字（**含负数/小数**，如 `-1`、`1.5`、`0.5`）/非空字符串/true 时才算失败；裸 `403` 不再匹配（"第403条"会误报），只匹配 `403 Forbidden` / `HTTP 403` / `status 403`
- **余额可视化编辑器数据源修复**：`/balance` 接口返回完整配置字段（url/api_key/api_user/quota_conversion/daily_quota/anchor_balance/refresh_time 等）且**包含禁用源**——编辑已有源时输入框正确回填、保存不再清空配置；保存不再静默删除禁用源（禁用源在列表中灰显标注「(禁用)」，不探测只显示缓存状态）
- **日志扫描不占用日志文件**：Windows 下用 `CreateFileW + FILE_SHARE_DELETE` 共享删除模式只读打开，句柄毫秒级释放，不阻塞 KiraAI 的 RotatingFileHandler 轮转 rename；修复 `f.tell()` 在文件关闭后调用导致游标不前进、重复计数的 bug
- **来源标签支持多填**：`source_default` / `source_group` / `source_dm` 从 string 改为 list（兼容旧配置单个字符串），第一个为主标签（实际归类用），其余作为备选/别名
- **WebUI 余额监测可视化编辑器**：侧边栏「余额监测」页新增「＋ 添加监测源」——点开后按类型（auto/custom/newapi/preset/daily/rolling）动态显示对应字段表单（网址/API Key/用户ID/换算比例/每日额度/刷新时刻/对表余额等），支持编辑/删除/启用开关，保存后自动热重载，无需再手写 JSON
- **WebUI 分类名中文化**：后台日志 ERROR 卡片分类显示中文（XML解析/模型调用/工具执行/网络超时/异常堆栈/其他），与工具摘要一致
- **轮转文件显式匹配**：`log.log` + `log.log.[0-9]*` 显式 glob（RotatingFileHandler 轮转命名），防未来轮转策略变化误扫无关文件；`get_data_path()` 返回值用 `Path()` 包裹防 str；`st_ino` 为 0（FAT32 等文件系统）时退化为路径跟踪，避免共用游标 0 重复计数

### v1.2.5（2026-08-31）

- **修复查询崩溃**：`_fmt_num` 千分位格式符 `N0` 需 Python 3.10+，低版本直接 ValueError 导致工具查询全挂，改为 `f"{v:,}"` 全版本兼容
- **修复渠道/模型显示未知**：`_resolve_channel_model` 读错层级（KiraAI 的 LLMModelClient 结构是 `client.model = ModelInfo`），改为正确读取 `model.model_id` / `model.provider_name` / `provider_config.base_url`，自动显示真实模型名与渠道名
- **余额轮询默认 5 分钟**（最小 1 分钟）；工具/命令查询余额时先即时探测，保证返回最新值（与 api-balance 插件行为一致）
- **余额配置模板化**：新增 DeepSeek / Kimi / 硅基流动 / 智谱四个官方平台快捷分区，填 API Key 即自动并入余额监测（对齐 api-balance 插件风格）；自定义/中转站仍走「余额监测源」高级 JSON
- **挂件独立小窗**：⧉ 按钮 `window.open` 弹出真正可拖动的独立挂件窗口（浏览器允许弹窗后生效，卡片填满窗口、自带关闭按钮）；内嵌模式拖动位置 localStorage 记忆，刷新/重开不丢；底部新增「新标签」直开链接（弹窗被拦截时的兜底）
- **New-API 简易文本格式**：对齐 api-balance 插件，配置页新增「New API 站点（简易文本格式）」分区，每行 `名称;base_url;令牌;用户ID;换算比例(可选)` 英文分号分隔，自动并入余额监测（type=newapi）
- **余额探测并发与超时加固**（姐姐审计）：`_probe_all` 网络型源改为**并行探测**（单源 8s 超时、整体 15s 超时），不再串行 N×10s 拖住工具/命令查询；工具/命令/API 查询余额时若后台轮询正忙会**等待其完成**（最多 20s）再返回最新值，不再拿旧状态；模板源插入顺序修正（append 保持配置顺序，不再反转）；`create_task` 替代 `ensure_future`（3.13 兼容）、pending 取消后 `gather` 等待（防 Task was destroyed 警告）、`balance_sources` 复制 cfg list 再 append（防热重载重复追加）

### v1.2.4（2026-08-31）

- **群聊默认来源标签 qchat → gm**：`source_group` 默认值对齐 KiraAI 框架会话类型标准（`qq:gm:xxx`），避免来源标签与 sid 命名不一致；schema/README/工具描述同步更新。已配置过 source_group 的用户不受影响（仅默认值变更）

### v1.2.3（2026-08-31）

- **修复 /trend 5 分钟下钻短路**（三轮审查 #1，核心功能 bug）：hour 分支排在 day+hour 分支之前且无条件拦截，5 分钟桶永远不可达。已在 day 分支条件加 `not hour_s`，`_mins` 数据结构恢复可用
- **修复 session_expire_minutes 单位混用**（三轮审查 #2）：`max(60, ...)` 误把分钟当秒比，配置 ≤60 分钟时一律 60 分钟才清理；改为 `max(1, ...) * 60`，配置如实生效
- **余额行按币种显示**（三轮审查 #3）：工具/命令出口的余额行不再一律用全局 `balance_unit`，积分制源（daily/rolling）显示「积分」
- **balance_state.json 顶层类型校验**（三轮审查 #4）：文件被写坏成数组/字符串时回退空 dict，不再 AttributeError；`_build_summary_text` 余额段整体包 try 兜底
- 回归测试：api_trend 五种分支场景、expire 计算、挂件 compact 注入逻辑全部断言通过

### v1.2.2（2026-08-31）

- **挂件紧凑模式真正生效**（二轮审查 A）：后端「紧凑模式」配置注入前端默认值；localStorage 有记忆时以用户为准，配置与前端操作不打架
- **会话状态自动清理**（二轮审查 C）：新增 `session_expire_minutes`（默认 30，可配置）——每个会话的来源继承文本与错误游标超过该时长无活动即清理，防长期运行内存缓慢增长
- **exact_match 文档如实化**（二轮审查 B）：schema hint 明确作用范围仅为「价格规则」匹配（URL/模型名/渠道名全字相等）；来源关键词归类与渠道 URL 探测不受影响
- **max_log_size 矛盾消除**：schema `minimum` 1000 → 0，与「0=不裁剪」文档一致
- **删除死代码**：`_ssl_connector`（从未使用）

### v1.2.1（2026-08-31）

- **修复时间戳兼容**（姐姐审查 #1）：日志统一写 6 位微秒，解析走 `_parse_ts` 兼容 3/6 位——Python 3.10- 的 `fromisoformat` 不再炸，`_apply_session` 主采集路径不再可能崩
- **修复错误统计重复计数**（姐姐审查 #2）：移植 errScanPos 位置游标（`_err_scan`），工具循环续轮同一段「出错：」只计一次，新响应自动重置
- **exact_match 配置生效**（姐姐审查 #3）：原为死代码，现模块级开关全量接入价格规则匹配器（URL/模型名/渠道名全字相等）
- **移除死代码**：`cmd_all_template`（从未使用）从 schema/代码删除
- **HTTPS 证书校验开关**：新增 `balance_ssl_verify`（默认关，兼容自签证书中转站；官方端点可开）
- **日志 IO 线程锁**：追加/裁剪/热读缓存统一走 `_IO_LOCK`，消除并发竞争面
- **`_range_agg` 统一遍历写法**：不再依赖 dict 插入序，与 `_range_cost_ex` 一致
- **data_dir 兜底**：`get_plugin_data_dir()` 返回 None 时降级插件目录，不再裸崩
- **新增 WebUI 悬浮挂件**（默认关闭）：侧边栏「Token 挂件」页——迷你卡片实时显示会话/今日 tokens、费用、余额，可拖动、可折叠成小球、紧凑模式，适合浏览器小窗钉角落；配置页「挂件」区开启

### v1.2.0（2026-08-31）

- **AI 查询函数扩容**：新增 `query_token_usage`（维度聚合：channel/model/source/day + 时间区间 + 关键字过滤 + top 上限）与 `query_token_records`（逐轮明细 + minInput 定位大上下文），输出带 4000 字符 ClampAiOutput 硬上限防回注撑爆上下文
- **热读缓存**：按 mtime+length 判失效，多端点轮询/查询不再重复全量读盘，大日志下性能显著提升
- **余额对表校准**：估算型源（preset/daily/rolling）支持 `anchor_balance`+`anchor_at`——填上游真实余额即校准，此后按价格规则自动扣减，吸收一切估算误差
- **每日重置/每日累计余额源**：新增 `daily`（每日额度 − 本周期计费，跨刷新锚定自动回落）与 `rolling`（设定余额 − 计费 + 每日发放结转滚存），`refresh_time` 纯时间推导不落状态，离线期间发放不丢
- **双币种计费**：价格规则新增 `currency` 字段（CNY/积分），费用按币种分桶累计永不混算，工具/命令/WebUI/API 全链路支持
- **时间趋势下钻**：WebUI 新增「时间趋势」页——按天柱状（Top8 模型费用分色堆叠 + 缓存命中率虚线），点日柱下钻单天按小时，点小时柱直达该小时逐轮记录
- **KPI 迷你走势线**：概览卡片加近 14 天用量走势 sparkline；概览页快照区按小时下钻入口
- WebUI 余额页标注估算型源、价格规则页新增币种列、记录页双币种费用显示

### v1.1.0（2026-08-31）

- **新增 `newapi` 余额源类型**：New-API 站点专属探测（请求带 `New-Api-User` 头，打 `/api/user/self`，自动从 quota/balance/remaining/points 等字段提取额度，按 `quota_conversion` 换算，默认 500000）
- **余额显示单位可自定义**：新增 `balance_unit` 配置（默认"元"，可改为"额度"/"美元"/"美刀"/"点数"等），工具/命令/WebUI 三端全局生效
- **requirements.txt**：新增依赖清单（aiohttp），支持插件管理自动安装依赖
- 配置文档与 README 同步更新

### v1.0.0（2026-08-30）

- 初始发布：Token 用量统计看板（逐轮采集/费用估算/余额监测/错误统计/WebUI 仪表盘/bot 工具/自定义命令）

</details>
