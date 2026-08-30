# KiraAI Token 用量统计（Token Stats）

为 KiraAI 提供完整的 **Token 用量统计看板**：逐轮记录输入/输出/缓存 tokens、按价格规则实时估算费用（峰谷价）、API 余额监测、出错统计——WebUI 侧边栏仪表盘 + bot 自然语言查询 + 可选自定义命令，三大入口全覆盖。

> 移植自 [Alife 的 TokenStats](https://github.com/1chuxin/1chuxin-Alife.TokenStats)（初心出品），并整合了 [KiraAI-plugin-api-balance](https://github.com/ChuXia2004/KiraAI-plugin-api-balance) 的查询模式与命令设计。
> **模型无关**：任何 Provider 只要在 LLMResponse 里上报 tokens 就能统计。

---

## ✨ 功能一览

| 功能 | 说明 |
|------|------|
| 📊 **逐轮采集** | 每轮 LLM 调用（含工具中间步）的 输入/输出/缓存 tokens，JSONL 落盘，重启不丢 |
| 💰 **费用估算** | 价格规则按 `URL > 模型 > 渠道名` 加权匹配（4/2/1 分），峰谷价（工作日 9-12 点、14-18 点为峰）；展示时实时计算，**改价后全历史即时重定价** |
| 💳 **余额监测** | 三类监测源：`auto`（按 URL 自动探测官方端点/One-API 中转站）、`custom`（自定义接口多端点尝试）、`preset`（初始额度 − 已计费用） |
| 🔍 **来源归类** | 自定义关键词规则优先 → 群聊/私聊自动判定 → 工具续轮继承上一轮，可自定义标签名 |
| 🖥️ **WebUI 仪表盘** | 侧边栏「Token 用量」页面：概览卡片（本次/今天/7天/30天/累计）、按天历史、今日按小时、维度分析（来源/渠道/模型/会话）、最近逐轮记录、价格规则、余额监测 |
| 🤖 **Bot 工具** | 自然语言问「用了多少 token / 花了多少钱 / 余额多少」自动触发 |
| ⌨️ **自定义命令** | 可选 `/用量` 等命令词直接查询（默认关闭，防打扰），支持用户白名单 |
| ⚠️ **出错统计** | 扫描 AI 输出中的「出错：」标记，按范围聚合展示 |

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
4. 若提示缺少 `aiohttp`（余额监测需要），在插件管理里点 **安装依赖**，或手动执行：
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

目录内需包含：`main.py`、`manifest.json`、`schema.json`、`icon.svg`、`icon-dark.svg`。

---

## 🚀 快速上手

### 1. 启用统计（默认已启用）

安装后插件即开始记录。发几条消息后打开 **侧边栏 → Token 用量** 即可看到数据。

### 2. 配置价格规则（可选，否则费用显示「—」）

插件管理 → `KiraAI_token_stats_plugin` → **配置 → 价格规则**。

预置了 DeepSeek V4-Flash / V4-Pro 官方峰谷价。自定义规则示例：

```json
[
  {
    "name": "我的中转站",
    "url_match": "myproxy.example.com",
    "peak_enabled": true,
    "hit_peak": 0.1,
    "hit_off": 0.05,
    "miss_peak": 3.0,
    "miss_off": 1.5,
    "out_peak": 9.0,
    "out_off": 4.5
  }
]
```

字段说明（价格单位：**元 / 百万 tokens**）：

| 字段 | 说明 |
|------|------|
| `name` | 规则名称（展示用） |
| `url_match` | 匹配 endpoint 域名（**推荐**，比渠道名稳定，双向包含匹配） |
| `model_match` | 匹配模型名（如 `flash`、`pro`） |
| `channel_match` | 匹配渠道名/主机名 |
| `peak_enabled` | 是否启用峰谷价（false = 恒按谷价） |
| `hit_peak` / `hit_off` | 缓存命中部分的单价（峰/谷） |
| `miss_peak` / `miss_off` | 未命中缓存部分的单价（峰/谷） |
| `out_peak` / `out_off` | 输出 tokens 单价（峰/谷） |

> 匹配规则取**加权分最高**的一条：URL 命中 +4 分、模型命中 +2 分、渠道名命中 +1 分（可叠加）。建议用 `url_match` 按域名配置——渠道重排/改名不受影响；同一域名下不同模型（如 flash/pro）用 `url_match + model_match` 组合精确区分。

### 3. 配置余额监测（可选）

插件配置页 → **余额监测** → 开启开关 → 添加监测源：

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
    "name": "备用金",
    "type": "preset",
    "initial": 100,
    "currency": "CNY",
    "enabled": false
  }
]
```

| 类型 | 行为 |
|------|------|
| `auto` | 按 URL 自动分流：DeepSeek → `/user/balance`；Moonshot/Kimi → `/v1/users/me/balance`；硅基流动 → `/v1/user/info`；智谱 → `/api/paas/v4/users/me/balance`；其他一律按 One-API/New-API 中转站探测（subscription − usage） |
| `custom` | 依次尝试常见余额接口（`/user/balance`、`/v1/users/me/balance`、`/v1/user/info`、`/api/paas/v4/users/me/balance`、One-API 组合）；接口特殊可填**完整余额接口 URL** + `json_path` 取数（如 `data.available_balance`） |
| `preset` | 无接口兜底：填 `initial`（初始额度），当前额度 = 初始额度 − 该渠道已计费用（按价格规则估算） |

> ⚠️ **安全提示**：`api_key` 以**明文**存储在插件配置文件（`data/config/plugins/KiraAI_token_stats_plugin.json`）中，请确保服务器文件权限安全。轮询间隔默认 60 分钟（最小 5 分钟）。

### 4. 让 bot 回答用量

启用 **Bot 工具**（默认开启）后，用户直接问：

> “今天用了多少 token？”
> “花了多少钱？”
> “余额还剩多少？”

bot 会自动调用 `query_token_stats` 工具并返回结果。

### 5. 自定义命令（可选，默认关闭）

插件配置页 → **自定义命令**：

1. `enable_command` 打开
2. `command_words` 添命令词（默认 `/用量`、`/token`，支持前缀匹配：`/用量 今天`）
3. 可选填写 `allowed_users` 白名单（留空 = 所有人可用）
4. 命令参数：`本次 / 今天 / 7天 / 30天 / 累计 / 余额`（留空 = 全部概览）

---

## 🖥️ WebUI 仪表盘

侧边栏 → **Token 用量**：

- **概览**：快照栏（来源/会话轮数/进行时长/最近一轮）+ 五个范围卡片（本次/今天/近7天/近30天/累计：总量、轮数、输入、输出、缓存、命中率、费用、出错）+ 按天历史（条形分布）+ 今日按小时柱状图
- **维度分析**：按 来源 / 渠道 / 模型 / 会话 四个维度聚合（可切 今天/7天/30天/累计）
- **最近记录**：最近 15 轮逐轮明细（时间/模型/来源/渠道/各 tokens/费用）
- **价格规则**：当前规则只读展示
- **余额监测**：各源余额/更新时间/状态，可手动「立即探测」

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
{"t":"2026-08-30T23:45:01.123","v":1234,"i":1000,"o":234,"c":500,"m":"deepseek-v4-flash","s":"qchat","ch":"api.deepseek.com","h":"api.deepseek.com","sid":"qq:gm:12345"}
```

| 字段 | 含义 |
|------|------|
| `t` | 时间戳 |
| `v` / `i` / `o` / `c` | 总量 / 输入 / 输出 / 缓存命中 tokens |
| `m` | 模型名 |
| `s` | 来源（qchat/dm/system/自定义） |
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
| 来源归类 | `source_default` / `source_group` / `source_dm` | `system` / `qchat` / `dm` | 兜底/群聊/私聊来源标签 |
| 自定义命令 | `enable_command` | `false` | 命令开关 |
| 自定义命令 | `command_words` | `["/用量","/token"]` | 命令词列表 |
| 自定义命令 | `allowed_users` | `[]` | 白名单（空=全部） |
| 自定义命令 | `exact_match` | `false` | 参数全字匹配 |
| 自定义命令 | `denied_message` | 权限不足… | 无权限提示 |
| 自定义命令 | `command_success_template` | `📊 {provider}：{result}` | 单结果模板 |
| 自定义命令 | `command_all_template` | `📊 Token 用量统计：\n{results}` | 汇总模板 |
| Bot 工具 | `enable_tool` | `true` | 工具开关 |
| Bot 工具 | `tool_include_balance` | `true` | 工具结果附带余额 |
| 价格规则 | `rules` | DeepSeek 官方价 | 价格规则数组 |
| 余额监测 | `enable_balance` | `false` | 余额监测开关 |
| 余额监测 | `balance_interval` | `60` | 轮询间隔分钟（≥5） |
| 余额监测 | `balance_sources` | `[]` | 监测源数组 |
| 高级设置 | `max_log_size` | `100000` | 日志保留条数（0=不裁剪） |
| 高级设置 | `session_idle_minutes` | `30` | 「本次会话」滚动窗口分钟数 |

---

## ❓ FAQ

**Q：费用显示「—」？**
A：没有匹配到价格规则。查看 价格规则 页确认规则是否覆盖你的模型/渠道，按域名（`url_match`）配置最稳。

**Q：为什么日志里没有我的模型名？**
A：模型名取自默认 LLM 客户端的 `model_id`/`model` 字段；如果你的 Provider 没暴露这些字段会显示「未知」。渠道识别同理，取 `base_url` 的域名，取不到显示「默认渠道」。

**Q：余额一直「尚未探测」？**
A：检查三点：① 余额监测开关已开启；② 源已 `enabled`；③ 轮询间隔到了（或用页面「立即探测」）。custom 源报错信息会给出具体原因（HTTP 状态/响应摘要）。

**Q：改价格后历史费用会变吗？**
A：会。费用**只在展示时计算**（日志只存 tokens/模型/渠道/时间戳），改价即时全历史重定价。

**Q：能统计多开/多 bot 实例吗？**
A：当前为单实例统计（汇总 + 按会话 sid 维度分析）。多实例并发写日志有文件锁保护，不会写坏。

**Q：aiohttp 没装会怎样？**
A：统计/工具/页面全部正常，仅余额监测不可用（加载时日志有提示）。

---

## 📄 许可证

[AGPL-3.0](LICENSE) — 修改后再分发需开源。

## 🙏 致谢

- [1chuxin/1chuxin-Alife.TokenStats](https://github.com/1chuxin/1chuxin-Alife.TokenStats) — 原版 Alife 插件（功能设计参考）
- [ChuXia2004/KiraAI-plugin-api-balance](https://github.com/ChuXia2004/KiraAI-plugin-api-balance) — 余额查询模式参考
