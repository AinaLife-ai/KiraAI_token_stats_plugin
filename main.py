# -*- coding: utf-8 -*-
"""KiraAI Token Stats — Token 用量统计看板插件

为 KiraAI 提供完整的 Token 用量统计能力（移植自 Alife 的 1chuxin.TokenStats 4.9.x 设计，
并整合 KiraAI-plugin-api-balance 的查询模式）：

- 逐轮采集：@on.llm_response 钩子记录每轮 LLM 调用的 输入/输出/缓存 tokens，
  包含工具中间步；日志 JSONL 持久化到插件数据目录，重启不丢
- 费用估算：价格规则按 URL > 模型 > 渠道名 加权匹配（4/2/1 分），
  峰谷价（工作日 9:00-12:00 / 14:00-18:00 为峰，其余谷）；
  费用一律在展示时计算，改价后全历史即时重定价；
  双币种（CNY 元 / 积分）分桶累计，永不混算
- 余额监测：auto（按 URL 自动分流官方端点 / One-API 中转站）、
  custom（自定义接口多端点尝试 + json_path 取数）、
  newapi（New-API 站点：New-Api-User 头 + /api/user/self + quota 换算）、
  preset（预设扣减钱包型）、daily（每日重置积分）、rolling（每日累计滚存积分）；
  估算型支持「当前余额(对表)」锚定：填上游实际余额即校准，此后按价格规则自动扣减
- 来源归类：自定义关键词规则优先 → 群聊/私聊自动判定 → 工具续轮继承上一轮
- 多入口查询：WebUI 侧边栏仪表盘（KPI 走势 + 时间趋势下钻）/ bot 工具
  （概览 / 维度聚合 / 逐轮明细）/ 可选自定义命令
- AI 查询函数：query_token_usage（维度聚合）与 query_token_records（逐轮明细），
  输出带 4000 字符硬上限防止回注结果撑爆上下文
- 热读缓存：按 mtime+length 判失效，大日志下轮询/查询不重复全量读盘
- 错误统计：LLM 响应内「出错：」正则扫描 + **后台日志（log.log）ERROR 行增量扫描**（分类聚合：XML解析/模型调用/工具执行/网络超时/异常堆栈）+ **工具结果失败钩子**（error/权限denied/超时/调用失败——LLM 白烧 token 的典型），按范围聚合

模型无关：统计基于 LLMResponse 的 input_tokens/output_tokens/cached_tokens 字段，
任何 Provider 只要上报 tokens 即可统计。
"""

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

try:
    import msvcrt  # Windows 共享删除模式打开日志（仅 NT 使用）
except Exception:  # pragma: no cover
    msvcrt = None

from fastapi import Request

from core.plugin import BasePlugin, logger, on, Priority, register
from core.plugin.plugin_registry import PluginPage, PageMenu
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.chat import MessageChain
from core.chat.message_elements import Text
from core.provider import LLMResponse, ToolResult

try:
    from core.utils.path_utils import get_data_path
    _HAS_DATA_PATH = True
except Exception:  # pragma: no cover
    get_data_path = None
    _HAS_DATA_PATH = False

try:
    import aiohttp
    from aiohttp.resolver import ThreadedResolver
    _HAS_AIOHTTP = True
except Exception:  # pragma: no cover
    aiohttp = None
    ThreadedResolver = None
    _HAS_AIOHTTP = False

# ────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────

RANGES = ("session", "today", "d7", "d30", "total")
RANGE_LABELS = {"session": "本次", "today": "今天", "d7": "近7天", "d30": "近30天", "total": "累计"}

ERROR_TAG_RE = re.compile(r"出错[：:]")

# ── 后台日志（log.log）ERROR 行扫描 ──
# 捕捉 KiraAI 控制台/日志文件里的真实错误，重点：LLM 输出错误 XML 格式导致解析失败
LOG_ERROR_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+ERROR\s+\[([^\]]+)\]\s*(.*)$")
# 分类：XML 解析失败（LLM 输出错误格式）→ 最关心的
LOG_ERR_XML_RE = re.compile(r"Error parsing message|Failed to fix xml|mismatched tag|unclosed token|no element found|unexpected end|junk after document")
# 分类：模型调用失败（Provider 层）
LOG_ERR_MODEL_RE = re.compile(r"Model .* failed|ProviderError|All models in the group failed")
# 分类：工具执行失败
LOG_ERR_TOOL_RE = re.compile(r"Tool '.*' timed out|Tool .* failed|tool execution failed|Permission denied|Access denied|权限不足|无权限|拒绝访问|Forbidden|not allowed")
# 分类：网络/超时
LOG_ERR_NET_RE = re.compile(r"超时|timed out|timeout|Connection error|Connection reset")
# 分类：Traceback（未分类异常堆栈）
LOG_ERR_TB_RE = re.compile(r"Traceback \(most recent call last\)")
LOG_ERR_CATS = ("xml", "model", "tool", "net", "traceback", "other")
LOG_ERR_LABELS = {"xml": "XML解析", "model": "模型调用", "tool": "工具执行", "net": "网络/超时", "traceback": "异常堆栈", "other": "其他"}

# ── 工具结果失败判定（on.tool_result 钩子）──
# tool 返回 error / 权限 denied / 超时 / 调用失败等——LLM 白烧 token 的典型
# 注意：error 字段值须为非零数字（含负数/小数）/非空字符串/true 才算失败
# （{"error": 0} 是很多 API 的成功约定）；不匹配裸 403（"第403条"会误报）
TOOL_ERR_RE = re.compile(
    r"\{['\"]?error['\"]?\s*:\s*(?:-?(?:[1-9]\d*(?:\.\d+)?|0\.\d*[1-9]\d*)|['\"](?![+-]?0+(?:\.0+)?['\"])[^'\"]+['\"]|true|True)\s*[,}]|"
    r"Error\s*:|Permission denied|Access denied|权限不足|无权限|拒绝访问|"
    r"Forbidden|HTTP\s*403|status\s*[=:]\s*403|not allowed|timed out|超时|"
    r"Failed to call tool|not implemented|"
    r"调用失败|执行失败|查询失败|获取失败|生成失败|发送失败|上传失败|下载失败|删除失败|保存失败|"
    r"失败[：:，。]|失败$",
    re.IGNORECASE)

# 本插件自身工具的正常输出前缀：摘要/聚合/明细文本里含"失败/出错"字样
# （如"工具结果失败：N 次"、"最近出错：…"）但并非工具执行失败，排除自报避免统计污染
_SELF_TOOL_RE = re.compile(r"^(?:【Token 用量统计】|【用量·|【最近轮次】)")

# LLM 响应内自报片段剥离：bot 转述本插件统计结果时带「出错：」字样
# （如"最近出错：Merge facts error"、"出错标记合计：3"、"工具结果失败：2 次"、
# "后台日志错误：…"），并非真实错误，扫描前剥离避免自增；
# 非贪婪+标点边界：只吃到句号/感叹号/问号/换行/下一个「出错」标记即停，
# 避免吞掉后面的真实错误；出错标记合计/工具结果失败是固定数字格式，精确匹配不吞内容
_SELF_REPORT_RE = re.compile(
    r"最近出错：[^\n]*?(?=[。！？\n]|出错[：:]|$)"
    r"|出错标记合计：\d+"
    r"|工具结果失败：\d+\s*次"
    r"|后台日志错误：[^\n]*?(?=[。！？\n]|出错[：:]|$)"
)

# AI 回注文本硬上限（正常结果 1-2K 字符，防御性兜底防 Poke 回注撑爆上下文）
AI_OUTPUT_LIMIT = 4000

# 估算型余额源类型 / 积分制类型
EST_TYPES = ("preset", "daily", "rolling")
POINT_TYPES = ("daily", "rolling")

# 内置默认价格规则（DeepSeek 官方价，2026-08 抓取；可在配置页修改）
DEFAULT_RULES = [
    {
        "name": "DeepSeek V4-Flash（官方价）",
        "model_match": "flash",
        "currency": "CNY",
        "peak_enabled": True,
        "hit_peak": 0.10, "hit_off": 0.05,
        "miss_peak": 3.0, "miss_off": 1.5,
        "out_peak": 9.0, "out_off": 4.5,
    },
    {
        "name": "DeepSeek V4-Pro（官方价）",
        "model_match": "pro",
        "currency": "CNY",
        "peak_enabled": True,
        "hit_peak": 0.30, "hit_off": 0.15,
        "miss_peak": 9.0, "miss_off": 4.5,
        "out_peak": 27.0, "out_off": 13.5,
    },
]

# 余额 auto 模式：官方端点分流域名关键字
_DS_HOSTS = ("deepseek",)
_MS_HOSTS = ("moonshot", "kimi")
_SF_HOSTS = ("siliconflow",)
_ZP_HOSTS = ("bigmodel", "zhipu")


# ────────────────────────────────────────────────────────────
# 工具函数
# ────────────────────────────────────────────────────────────

def _fmt_num(v):
    """千分位格式化（兼容 Python <3.10：N 格式符 3.10+ 才有）"""
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        return "0"
    return f"{v:,}"


def _fmt4(v):
    """超长自动缩写为 K/M/B（挂件风格）"""
    try:
        v = max(0, int(v or 0))
    except (TypeError, ValueError):
        return "0"
    if v < 1000:
        return str(v)
    if v < 9950:
        return f"{v/1000:.1f}".replace(".0", "") + "K"
    if v < 995000:
        return str(round(v / 1000)) + "K"
    if v < 9950000:
        return f"{v/1000000:.1f}".replace(".0", "") + "M"
    if v < 995000000:
        return str(round(v / 1000000)) + "M"
    if v < 9950000000:
        return f"{v/1000000000:.1f}".replace(".0", "") + "B"
    return str(round(v / 1000000000)) + "B"


def _is_peak(t: datetime) -> bool:
    if t.weekday() >= 5:
        return False
    h = t.hour
    return (9 <= h < 12) or (14 <= h < 18)


def _url_loose_match(candidate: str, pattern: str) -> bool:
    if not candidate or not pattern:
        return False
    c, p = candidate.lower(), pattern.lower()
    return p in c or c in p


# exact_match 全字匹配开关（插件 __init__ 时注入，默认 False=包含匹配）
_EXACT_MATCH = False


def _match_rule(rules: list, channel: str, model: str, url: str):
    """URL > 模型 > 渠道名 加权匹配：4/2/1 分，取最高分。
    _EXACT_MATCH=True 时匹配需全字相等（区分大小写），否则包含匹配"""
    exact = _EXACT_MATCH
    best, best_score = None, 0
    for r in rules or []:
        score = 0
        if exact:
            if r.get("url_match") and str(url or "") == str(r.get("url_match") or ""):
                score += 4
            if r.get("model_match") and str(model or "") == str(r.get("model_match") or ""):
                score += 2
            if r.get("channel_match") and str(channel or "") == str(r.get("channel_match") or ""):
                score += 1
        else:
            if r.get("url_match") and _url_loose_match(url or "", r.get("url_match") or ""):
                score += 4
            if r.get("model_match") and model and r["model_match"].lower() in str(model).lower():
                score += 2
            if r.get("channel_match") and channel and r["channel_match"].lower() in str(channel).lower():
                score += 1
        if score > best_score:
            best_score, best = score, r
    return best


def _rule_currency(r: dict) -> str:
    """规则计价币种：'积分'=积分/百万tokens，其余一律 CNY"""
    cur = (r or {}).get("currency") or "CNY"
    return "积分" if str(cur).strip() == "积分" else "CNY"


def _rule_cost_ex(r: dict, input_t: int, output_t: int, cached_t: int, t: datetime):
    """按规则算费用 → (金额, 币种)；无规则/未匹配返回 (None, 'CNY')"""
    if r is None:
        return None, "CNY"
    peak = bool(r.get("peak_enabled", True)) and _is_peak(t)
    hit = r.get("hit_peak" if peak else "hit_off", 0) or 0
    miss = r.get("miss_peak" if peak else "miss_off", 0) or 0
    out = r.get("out_peak" if peak else "out_off", 0) or 0
    amt = (cached_t * hit + max(0, input_t - cached_t) * miss + output_t * out) / 1_000_000
    return amt, _rule_currency(r)


def _rule_cost(r: dict, input_t: int, output_t: int, cached_t: int, t: datetime) -> float:
    """单币种包装（仅 CNY 金额，用于旧接口兼容）"""
    amt, _ = _rule_cost_ex(r, input_t, output_t, cached_t, t)
    return amt


# 文件 IO 线程锁（日志追加/裁剪/热读缓存并发保护）
_IO_LOCK = threading.RLock()


def _read_jsonl(path: Path):
    recs = []
    if not path.exists():
        return recs
    try:
        with _IO_LOCK:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return recs


def _append_jsonl(path: Path, rec: dict, max_size: int = 0):
    try:
        with _IO_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # 裁剪：超过 max_size 条时保留最新（0 = 不裁剪）
            if max_size and max_size > 0:
                lines = path.read_text(encoding="utf-8").splitlines()
                if len(lines) > max_size:
                    path.write_text("\n".join(lines[-max_size:]) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning(f"[token_stats] 日志写入失败: {e}")


def _parse_ts(s: str):
    """日志时间戳解析：兼容 3/6 位微秒（Python 3.10- 的 fromisoformat 只认 6 位）"""
    if not s:
        raise ValueError("empty timestamp")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    if "." in s:
        try:
            head, frac = s.split(".", 1)
            frac = (frac + "000000")[:6]
            return datetime.fromisoformat(f"{head}.{frac}")
        except ValueError:
            pass
    raise ValueError(f"bad timestamp: {s}")


def _fmt_ts(dt: datetime) -> str:
    """日志时间戳写入：统一 6 位微秒，避免低版本 Python 解析失败"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _err_scan(full_text: str, prev):
    """errScanPos：位置游标错误计数。
    传入 (prev_text, prev_end)；新文本若以旧文本为前缀（续写/追加），
    只统计新增段，工具循环同一段「出错：」不重复计数；
    全新响应（前缀不匹配）整段重数并重置游标。
    返回 (新增计数, 新游标状态)。"""
    if prev is None:
        n = len(ERROR_TAG_RE.findall(full_text))
        return n, (full_text, len(full_text))
    prev_text, prev_end = prev
    if len(full_text) >= prev_end and full_text[:prev_end] == prev_text[:prev_end]:
        return len(ERROR_TAG_RE.findall(full_text[prev_end:])), (full_text, len(full_text))
    n = len(ERROR_TAG_RE.findall(full_text))
    return n, (full_text, len(full_text))


def _clamp_ai_output(s: str) -> str:
    """AI 回注结果硬上限，防止 Poke 回注撑爆上下文"""
    if len(s) <= AI_OUTPUT_LIMIT:
        return s
    return s[:AI_OUTPUT_LIMIT - 60] + "\n…（结果过长已截断，请缩小范围或减少 top/n）"


def _parse_hhmm(s, default_h=0, default_m=0):
    """解析 HH:mm → (时, 分)；失败返回默认"""
    try:
        h, m = str(s or "").strip().split(":")
        return int(h), int(m)
    except Exception:
        return default_h, default_m


def _last_refresh(now: datetime, refresh_time: str) -> datetime:
    """上次刷新时刻（纯时间推导，不落状态）：客户端离线期间发放不丢"""
    h, m = _parse_hhmm(refresh_time)
    today_refresh = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now >= today_refresh:
        return today_refresh
    return today_refresh - timedelta(days=1)


def _grant_count(since: datetime, now: datetime, refresh_time: str) -> int:
    """since（严格大于）之后到 now 的刷新发放次数"""
    h, m = _parse_hhmm(refresh_time)
    count = 0
    t = since.replace(hour=h, minute=m, second=0, microsecond=0)
    while t <= now:
        if t > since:
            count += 1
        t += timedelta(days=1)
    return count
# ────────────────────────────────────────────────────────────
# 插件主类
# ────────────────────────────────────────────────────────────

class TokenStatsPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        # ── 基础 ──
        basic = cfg.get("section_basic", {})
        self.enabled = basic.get("enabled", True)
        self.debug_log = bool(basic.get("debug_log", False))
        self.source_rules = basic.get("source_rules", {}) or {}
        if not isinstance(self.source_rules, dict):
            self.source_rules = {}

        # ── 来源归类 ──
        src = cfg.get("section_source", {})
        # 默认来源：支持 string（旧配置兼容）或 list（多标签，取第一个为主标签）
        self.source_default = self._src_label(src.get("source_default", "system"), "system")
        # 群聊默认标签对齐 KiraAI 会话类型（qq:gm:xxx），而非自定义的 qchat
        self.source_group = self._src_label(src.get("source_group", "gm"), "gm")
        self.source_dm = self._src_label(src.get("source_dm", "dm"), "dm")

        # ── 自定义命令 ──
        cmd = cfg.get("section_command", {})
        self.enable_command = bool(cmd.get("enable_command", False))
        self.command_words = cmd.get("command_words", ["/用量", "/token"]) or ["/用量"]
        self.allowed_users = [str(u).strip() for u in (cmd.get("allowed_users", []) or []) if str(u).strip()]
        self.exact_match = bool(cmd.get("exact_match", False))
        self.denied_message = cmd.get("denied_message", "权限不足：您没有查询用量统计的权限")
        self.cmd_success_template = cmd.get("command_success_template", "📊 {provider}：{result}")

        # ── Bot 工具 ──
        tool = cfg.get("section_tool", {})
        self.enable_tool = bool(tool.get("enable_tool", True))
        self.tool_include_balance = bool(tool.get("tool_include_balance", True))

        # ── 价格规则 ──
        pr = cfg.get("section_pricing", {})
        rules = pr.get("rules", None)
        # 空数组也保留（用户删光规则 → 费用显示「—」），仅 None/非 list 回退默认
        self.rules = rules if isinstance(rules, list) else DEFAULT_RULES

        # ── 余额监测 ──
        bal = cfg.get("section_balance", {})
        self.enable_balance = bool(bal.get("enable_balance", False))
        interval = bal.get("balance_interval", 5)
        self.balance_interval = max(1, int(interval) if interval is not None else 5)
        sources = bal.get("balance_sources", [])
        # 复制一份再 append，避免直接引用 cfg 原始 list（热重载时重复追加）
        self.balance_sources = list(sources) if isinstance(sources, list) else []
        # New-API 站点简易文本格式（对齐 api-balance 插件）：每行 名称;base_url;令牌;用户ID;换算比例(可选)
        simple_sec = cfg.get("section_balance_newapi_simple", {}) or {}
        simple_list = simple_sec.get("newapi_sites_simple", [])
        if isinstance(simple_list, list):
            for line in simple_list:
                if not line or not str(line).strip():
                    continue
                parts = [p.strip() for p in str(line).split(";")]
                if len(parts) < 4:
                    continue
                name = parts[0] or "未命名站点"
                base_url = parts[1].rstrip("/")
                api_key = parts[2]
                api_user = parts[3]
                conversion = parts[4] if len(parts) >= 5 and parts[4].strip() else "500000"
                try:
                    conversion = float(conversion)
                except (TypeError, ValueError):
                    conversion = 500000
                if not base_url or not api_key or not api_user:
                    continue
                self.balance_sources.append({
                    "name": name,
                    "type": "newapi",
                    "url": base_url,
                    "api_key": api_key,
                    "api_user": api_user,
                    "quota_conversion": conversion,
                    "enabled": True,
                })
        # 固定平台快捷配置（对齐 api-balance 插件风格）：启用 + 填 key 即自动并入余额源
        for key, dname, durl in (
                ("deepseek", "DeepSeek", "https://api.deepseek.com"),
                ("moonshot", "月之暗面 Kimi", "https://api.moonshot.cn/v1"),
                ("siliconflow", "硅基流动", "https://api.siliconflow.cn"),
                ("zhipu", "智谱", "https://open.bigmodel.cn/api/paas/v4")):
            sec = cfg.get(f"section_balance_{key}", {}) or {}
            if sec.get("enabled") and sec.get("api_key"):
                self.balance_sources.append({
                    "name": (sec.get("name") or dname).strip(),
                    "type": "auto",
                    "url": (sec.get("base_url") or durl).strip(),
                    "api_key": sec.get("api_key"),
                    "enabled": True,
                })
        self.balance_unit = (bal.get("balance_unit", "元") or "元").strip() or "元"

        # ── 高级 ──
        adv = cfg.get("section_advanced", {})
        max_log = adv.get("max_log_size", 100000)
        self.max_log_size = int(max_log) if max_log is not None else 100000
        idle = adv.get("session_idle_minutes", 30)
        self.session_idle_minutes = max(1, int(idle) if idle is not None else 30)
        expire = adv.get("session_expire_minutes", 30)
        # 会话内临时状态（来源继承/错误游标）无活动清理时间，秒；最小 1 分钟
        self.session_expire_seconds = max(1, int(expire) if expire is not None else 30) * 60

        # ── 挂件（WebUI 悬浮小卡片，默认关闭）──
        wid = cfg.get("section_widget", {})
        self.enable_widget = bool(wid.get("enable_widget", False))
        self.widget_compact = bool(wid.get("widget_compact", False))

        # ── 余额探测 ssl ──
        self.balance_ssl_verify = bool(bal.get("balance_ssl_verify", False))

        # ── 运行时状态 ──
        self._data_dir: Path = None  # initialize 时赋值
        self._log_path: Path = None
        self._lock = asyncio.Lock()

        # 按天聚合：{day: {r,v,i,o,c,e, aggs:{model\u001Fchannel\u001Fhost:[off,peak]}}}
        self._days = {}
        # 单天小时桶：{day: [None]*24 each {r,v,i,o,c,e, aggs}}（aggs 供小时级费用分色）
        self._hours = {}
        # 5 分钟桶：{day: [None]*24 each [None]*12 each {r,v,i,o,c,e, aggs}}（时间趋势最深层下钻）
        self._mins = {}
        # 会话窗口（滚动）：
        self._sess = {
            "start": time.time(), "last": time.time(),
            "r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0,
            "aggs": {},
        }
        self._last_err_text = ""
        self._last_err_at = None
        self._cur_source = self.source_default
        self._cur_channel = "默认渠道"
        self._cur_model = "未知"
        self._last_round = {"i": 0, "o": 0, "c": 0}

        # 来源继承：{sid: {"text": str, "source": str, "steps": int, "at": float}}
        self._pending = {}

        # 余额状态：{name: {balance,currency,at,ok,msg}}
        self._bal_states = {}
        self._bal_busy = False
        self._bal_task: asyncio.Task = None

        # 热读缓存（4.9.x）：按 mtime+length 判失效；多端点轮询共享
        self._rec_cache = {"path": None, "mtime": None, "len": -1, "list": None}
        self._rec_cache_lock = asyncio.Lock()

        # 出错统计游标（errScanPos）：{sid: (prev_text, prev_end)}，工具循环续轮不重复计数
        self._err_cursor = {}

        # 后台日志（log.log）ERROR 扫描状态（按文件身份 st_ino 跟踪游标，轮转改名不丢不重）
        self._log_err_inodes = {}          # {st_ino: pos} 文件身份 → 字节游标
        self._log_err_hist = {}            # {day: {cat: count}} 按天分类聚合
        self._log_err_last = {"at": "", "cat": "", "text": ""}  # 最近一条 ERROR
        self._log_err_task: asyncio.Task = None
        self._log_err_lock = asyncio.Lock()
        self._err_save_at = 0.0            # 错误统计持久化节流时间戳

        # 工具结果失败统计（on.tool_result 钩子）：{day: count}
        self._tool_err_hist = {}
        # 注：ToolResult 无工具名字段，只记时间+文本
        self._tool_err_last = {"at": "", "text": ""}

        # exact_match 透传：模块级 flag（单实例插件可接受），匹配器全插件生效
        global _EXACT_MATCH
        _EXACT_MATCH = self.exact_match

    # ── 生命周期 ──

    async def initialize(self):
        self._data_dir = self.ctx.get_plugin_data_dir()
        if self._data_dir is None:
            # data_dir 不可用时兜底到插件包目录（只读退化，避免整个插件挂掉）
            self._data_dir = Path(__file__).resolve().parent
            logger.warning("[token_stats] get_plugin_data_dir() 返回 None，降级使用插件目录")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._data_dir / "usage-log.jsonl"
        self._bal_state_path = self._data_dir / "balance_state.json"

        self._load_history()
        self._load_bal_states()
        self._load_err_stats()

        # 后台日志 ERROR 扫描：扫描 data 目录下 log.log*（含轮转文件），按 ino 增量
        self._log_err_task = asyncio.create_task(self._log_err_loop())
        logger.info("[token_stats] 后台日志 ERROR 扫描已启动（log.log* 含轮转）")

        if self.debug_log:
            logger.info(f"[token_stats] init: rules={len(self.rules)} balance_sources={len(self.balance_sources)} "
                        f"command={'on' if self.enable_command else 'off'} tool={'on' if self.enable_tool else 'off'}")

        # 余额轮询后台任务
        if self.enable_balance and self.balance_sources and _HAS_AIOHTTP:
            self._bal_task = asyncio.create_task(self._balance_loop())
            logger.info(f"[token_stats] 余额轮询已启动（间隔 {self.balance_interval} 分钟）")
        elif self.enable_balance and self.balance_sources and not _HAS_AIOHTTP:
            logger.warning("[token_stats] aiohttp 未安装，余额监测不可用（pip install aiohttp）")

        logger.info("[token_stats] Token 用量统计已就绪")

    async def terminate(self):
        if self._bal_task and not self._bal_task.done():
            self._bal_task.cancel()
            try:
                await self._bal_task
            except asyncio.CancelledError:
                pass
            self._bal_task = None
        if self._log_err_task and not self._log_err_task.done():
            self._log_err_task.cancel()
            try:
                await self._log_err_task
            except asyncio.CancelledError:
                pass
            self._log_err_task = None
        # 退出前强制落盘错误统计（绕过节流，热重载/重启不丢）
        self._maybe_save_err_stats(force=True)

    # ── 历史加载 ──

    def _load_history(self):
        self._days.clear()
        self._hours.clear()
        self._mins.clear()
        for rec in self._read_records():
            self._apply_rec(rec)
        logger.info(f"[token_stats] 已加载历史 {len(self._days)} 天 / {sum(d['v'] for d in self._days.values())} tokens")

    def _read_records(self):
        """带热读缓存的日志读取：mtime+length 判失效（主日志只追加，安全）"""
        if self._log_path is None:
            return []
        try:
            mtime = self._log_path.stat().st_mtime
            length = self._log_path.stat().st_size
        except Exception:
            mtime, length = None, -1
        c = self._rec_cache
        if c["path"] == str(self._log_path) and c["mtime"] == mtime and c["len"] == length and c["list"] is not None:
            return c["list"]
        recs = _read_jsonl(self._log_path)
        c["path"] = str(self._log_path)
        c["mtime"] = mtime
        c["len"] = length
        c["list"] = recs
        return recs

    def _invalidate_rec_cache(self):
        self._rec_cache["list"] = None

    def _apply_rec(self, rec: dict):
        try:
            t = _parse_ts(rec.get("t", ""))
        except Exception:
            return
        day = t.strftime("%Y-%m-%d")
        v = int(rec.get("v", 0) or 0)
        i = int(rec.get("i", 0) or 0)
        o = int(rec.get("o", 0) or 0)
        c = int(rec.get("c", 0) or 0)
        e = int(rec.get("e", 0) or 0)
        peak = _is_peak(t)
        key = f"{rec.get('m', '')}\u001F{rec.get('ch', '')}\u001F{rec.get('h', '')}"

        ds = self._days.setdefault(day, {"r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0, "aggs": {}})
        ds["r"] += 1; ds["v"] += v; ds["i"] += i; ds["o"] += o; ds["c"] += c; ds["e"] += e
        slots = ds["aggs"].setdefault(key, [None, None])
        agg = slots[1 if peak else 0]
        if agg is None:
            agg = slots[1 if peak else 0] = {"i": 0, "o": 0, "c": 0}
        agg["i"] += i; agg["o"] += o; agg["c"] += c

        hs = self._hours.setdefault(day, [None] * 24)
        hr = hs[t.hour]
        if hr is None:
            hr = hs[t.hour] = {"r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0, "aggs": {}}
        hr["r"] += 1; hr["v"] += v; hr["i"] += i; hr["o"] += o; hr["c"] += c; hr["e"] += e
        hslots = hr["aggs"].setdefault(key, [None, None])
        hagg = hslots[1 if peak else 0]
        if hagg is None:
            hagg = hslots[1 if peak else 0] = {"i": 0, "o": 0, "c": 0}
        hagg["i"] += i; hagg["o"] += o; hagg["c"] += c

        # 5 分钟桶（时间趋势最深层下钻）
        m5 = t.minute // 5
        ms = self._mins.setdefault(day, [None] * 24)
        mrow = ms[t.hour]
        if mrow is None:
            mrow = ms[t.hour] = [None] * 12
        mb = mrow[m5]
        if mb is None:
            mb = mrow[m5] = {"r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0, "aggs": {}}
        mb["r"] += 1; mb["v"] += v; mb["i"] += i; mb["o"] += o; mb["c"] += c; mb["e"] += e
        mslots = mb["aggs"].setdefault(key, [None, None])
        magg = mslots[1 if peak else 0]
        if magg is None:
            magg = mslots[1 if peak else 0] = {"i": 0, "o": 0, "c": 0}
        magg["i"] += i; magg["o"] += o; magg["c"] += c

    def _apply_session(self, rec: dict):
        now = time.time()
        # 会话窗口滚动：超过 idle 分钟无新记录 → 重置
        if now - self._sess["last"] > self.session_idle_minutes * 60:
            self._sess = {
                "start": now, "last": now,
                "r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0, "aggs": {},
            }
        self._sess["last"] = now
        s = self._sess
        s["r"] += 1
        s["v"] += rec["v"]; s["i"] += rec["i"]; s["o"] += rec["o"]; s["c"] += rec["c"]
        s["e"] += rec.get("e", 0)
        key = f"{rec.get('m', '')}\u001F{rec.get('ch', '')}\u001F{rec.get('h', '')}"
        slots = s["aggs"].setdefault(key, [None, None])
        try:
            rec_t = _parse_ts(rec["t"])
        except Exception:
            rec_t = datetime.now()
        peak = _is_peak(rec_t)
        agg = slots[1 if peak else 0]
        if agg is None:
            agg = slots[1 if peak else 0] = {"i": 0, "o": 0, "c": 0}
        agg["i"] += rec["i"]; agg["o"] += rec["o"]; agg["c"] += rec["c"]

    # ── 余额状态 ──

    def _load_bal_states(self):
        try:
            if self._bal_state_path.exists():
                data = json.loads(self._bal_state_path.read_text(encoding="utf-8"))
                # 顶层类型校验：文件被写坏成数组/字符串时回退空 dict，避免后续 .get 崩
                self._bal_states = data if isinstance(data, dict) else {}
        except Exception:
            self._bal_states = {}

    def _save_bal_states(self):
        try:
            self._bal_state_path.write_text(
                json.dumps(self._bal_states, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ── 来源 / 渠道识别 ──

    @staticmethod
    def _src_label(v, fallback: str) -> str:
        """来源标签归一化：string 直接用；list 取第一个非空项（多标签时主标签）；
        空/非法回退默认。"""
        if isinstance(v, list):
            for item in v:
                s = str(item or "").strip()
                if s:
                    return s
            return fallback
        s = str(v or "").strip()
        return s or fallback

    def _classify_source(self, sid: str, event) -> str:
        pending = self._pending.get(sid)
        text = (pending or {}).get("text", "") or ""
        # 自定义关键词规则优先
        if self.source_rules:
            lowered = text.lower()
            for kw, src in self.source_rules.items():
                if kw and kw.lower() in lowered:
                    return str(src)
        # 自动判定
        try:
            if event.is_group_message():
                return self.source_group
            return self.source_dm
        except Exception:
            pass
        return self.source_default

    def _resolve_channel_model(self):
        """从默认 LLM 客户端取 provider 名/模型名/host（KiraAI 结构：client.model = ModelInfo）
        防御式，失败回退默认值"""
        channel, model, host = "默认渠道", "未知", ""
        try:
            client = self.ctx.get_default_llm_client()
            mi = getattr(client, "model", None)
            if mi is not None:
                model = getattr(mi, "model_id", None) or "未知"
                pname = getattr(mi, "provider_name", None) or ""
                pcfg = getattr(mi, "provider_config", None) or {}
                if not isinstance(pcfg, dict):
                    pcfg = {}
                base_url = (pcfg.get("base_url") or pcfg.get("baseUrl")
                            or pcfg.get("url") or pcfg.get("endpoint") or "")
                if base_url:
                    try:
                        host = urlparse(base_url).hostname or ""
                    except Exception:
                        host = ""
                channel = pname or host or "默认渠道"
            else:
                # 兜底：直接属性
                model = (getattr(client, "model_id", None)
                         or getattr(client, "model_name", None) or "未知")
                mcfg = getattr(client, "model_config", None) or {}
                if not isinstance(mcfg, dict):
                    mcfg = {}
                base_url = (mcfg.get("base_url") or mcfg.get("baseUrl")
                            or mcfg.get("url") or mcfg.get("endpoint") or "")
                if base_url:
                    try:
                        host = urlparse(base_url).hostname or ""
                    except Exception:
                        host = ""
                channel = (getattr(client, "provider_id", None)
                           or getattr(client, "provider_name", None) or host or "默认渠道")
        except Exception:
            pass
        return channel, model, host

    def _sid(self, event) -> str:
        sid = getattr(event, "sid", None)
        if sid:
            return sid
        try:
            return event.session.sid
        except Exception:
            return "default"

    def _is_allowed(self, user_id: str) -> bool:
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    def _sweep_stale_sessions(self):
        """清理长期无活动的会话临时状态（来源继承/错误游标），防内存缓慢增长"""
        try:
            cutoff = time.time() - self.session_expire_seconds
            stale = [sid for sid, p in self._pending.items() if p.get("at", 0) < cutoff]
            for sid in stale:
                self._pending.pop(sid, None)
                self._err_cursor.pop(sid, None)
        except Exception:
            pass

    # ── 事件钩子 ──

    @on.im_message(priority=Priority.HIGH)
    async def on_im_message(self, event: KiraMessageEvent, *_):
        """捕获用户文本（来源归类用）+ 自定义命令处理"""
        sid = self._sid(event)
        text = "".join(e.text for e in event.message.chain if isinstance(e, Text))
        if text:
            self._sweep_stale_sessions()
            self._pending[sid] = {"text": text, "source": None, "steps": 0, "at": time.time(), "new_msg": True}

        if not self.enable_command:
            return
        if not text:
            return
        text = text.strip()

        matched = False
        for cmd in self.command_words:
            if text == cmd or text.startswith(cmd + " "):
                matched = True
                break
        if not matched:
            return

        user_id = ""
        try:
            user_id = str(event.message.sender.user_id)
        except Exception:
            pass
        if not user_id:
            return
        if not self._is_allowed(user_id):
            await self.ctx.message_processor.send_message_chain(
                sid, MessageChain([Text(self.denied_message)]))
            event.discard(force=True)
            event.stop()
            return

        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        reply = await self._build_query_reply(arg)
        await self.ctx.message_processor.send_message_chain(sid, MessageChain([Text(reply)]))
        event.discard(force=True)
        event.stop()

    @on.llm_response(priority=Priority.LOW)
    async def on_llm_response(self, event, resp: LLMResponse, *_):
        if not self.enabled:
            return
        inp = resp.input_tokens
        out = resp.output_tokens
        if inp is None and out is None:
            return
        inp = int(inp or 0)
        out = int(out or 0)
        cached = int(resp.cached_tokens or 0)
        if inp == 0 and out == 0:
            return

        sid = self._sid(event)

        # 错误统计（errScanPos：位置游标，工具循环同一段「出错：」不重复计数）
        text = (resp.text_response or "") or ""
        pending = self._pending.get(sid)
        if pending is None:
            pending = self._pending[sid] = {"text": "", "source": None, "steps": 0, "at": time.time()}
        else:
            pending["at"] = time.time()  # 活动触碰：续轮不视为过期
        # 剥离本插件统计结果的自报片段（bot 转述"最近出错：…/出错标记合计：N"不算真实错误）
        text = _SELF_REPORT_RE.sub("", text)
        # 剥离用户消息原文（bot 引用/复述用户原话时不算错误）：
        # 仅当用户原话含「出错」且长度 >= 4 才剥离，避免误删 bot 自己的真实错误
        user_text = (pending.get("text") or "").strip()
        if user_text and len(user_text) >= 4 and "出错" in user_text:
            text = text.replace(user_text, "")
        errs, self._err_cursor[sid] = _err_scan(text, self._err_cursor.get(sid))
        if errs > 0:
            self._last_err_text = self._err_snippet(text)
            self._last_err_at = datetime.now()

        # 来源：第一轮（新用户消息）自动判定，工具续轮继承
        # 新用户消息到达（on_im_message 置位）：重置步数重新判定——
        # 关键词规则按「消息包含关键词」逐条生效，而非会话首条消息锁定
        if pending.get("new_msg"):
            pending["steps"] = 0
            pending["new_msg"] = False
        pending["steps"] += 1
        if pending["steps"] <= 1 or pending.get("source") is None:
            src = self._classify_source(sid, event)
        else:
            src = pending.get("source", self.source_default)
        pending["source"] = src
        self._cur_source = src

        channel, model, host = self._resolve_channel_model()
        self._cur_channel = channel
        self._cur_model = model

        now = datetime.now()
        rec = {
            "t": _fmt_ts(now),
            "v": inp + out,
            "i": inp, "o": out, "c": cached,
            "m": model, "s": src, "ch": channel, "h": host,
            "sid": sid,
        }
        if errs > 0:
            rec["e"] = errs

        self._last_round = {"i": inp, "o": out, "c": cached}

        async with self._lock:
            self._apply_rec(rec)
            self._apply_session(rec)
            _append_jsonl(self._log_path, rec, self.max_log_size)
            self._invalidate_rec_cache()

        if self.debug_log:
            logger.info(f"[token_stats] rec: +{inp}in/{out}out/{cached}cache "
                        f"src={src} ch={channel} model={model}")

    @on.tool_result(priority=Priority.LOW)
    async def on_tool_result(self, event, result: ToolResult, *_):
        """工具结果失败统计：error/权限denied/超时/调用失败等——LLM 白烧 token 的典型"""
        if not self.enabled:
            return
        text = (getattr(result, "text", "") or "") or ""
        if not text:
            return
        # 排除本插件自身工具的正常输出：摘要/聚合/明细文本里含"失败/出错"字样
        # （如"工具结果失败：N 次"、"最近出错：…"）但并非工具执行失败，避免自报污染统计
        if _SELF_TOOL_RE.match(text):
            return
        if not TOOL_ERR_RE.search(text):
            return
        day = datetime.now().strftime("%Y-%m-%d")
        self._tool_err_hist[day] = self._tool_err_hist.get(day, 0) + 1
        self._tool_err_last = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "text": text.replace("\r", " ").replace("\n", " ").strip()[:160],
        }
        if self.debug_log:
            logger.info(f"[token_stats] 工具结果失败 +1: {self._tool_err_last['text'][:60]}")

    @staticmethod
    def _err_snippet(text: str) -> str:
        idx = text.find("出错")
        if idx < 0:
            return ""
        start = max(0, idx - 20)
        s = text[start:idx + 40].replace("\r", " ").replace("\n", " ").strip()
        return s[:60]
    # ── 聚合查询（双币种）──

    def _range_agg(self, from_date: str, to_date: str):
        """按天键区间聚合 {v,i,o,c,r,e}（全遍历 + 区间判断，不依赖插入序）"""
        v = i = o = c = r = e = 0
        for key, ds in self._days.items():
            if from_date <= key <= to_date:
                v += ds["v"]; i += ds["i"]; o += ds["o"]; c += ds["c"]
                r += ds["r"]; e += ds["e"]
        return {"v": v, "i": i, "o": o, "c": c, "r": r, "e": e}

    def _aggs_cost_ex(self, aggs: dict):
        """聚合桶 → (cny_total, pts_total, matched)。双币种分桶累计，永不混算"""
        cny, pts, matched = 0.0, 0.0, False
        for mkey, slots in aggs.items():
            parts = mkey.split("\u001F")
            model = parts[0] if len(parts) > 0 else ""
            channel = parts[1] if len(parts) > 1 else ""
            host = parts[2] if len(parts) > 2 else ""
            rule = _match_rule(self.rules, channel, model, host)
            if rule is None:
                continue
            cur = _rule_currency(rule)
            for is_peak, agg in ((False, slots[0]), (True, slots[1])):
                if agg is None:
                    continue
                pk = bool(rule.get("peak_enabled", True)) and is_peak
                hit = rule.get("hit_peak" if pk else "hit_off", 0) or 0
                miss = rule.get("miss_peak" if pk else "miss_off", 0) or 0
                out = rule.get("out_peak" if pk else "out_off", 0) or 0
                amt = (agg["c"] * hit + max(0, agg["i"] - agg["c"]) * miss + agg["o"] * out) / 1_000_000
                if cur == "积分":
                    pts += amt
                else:
                    cny += amt
                matched = True
        return cny, pts, matched

    def _range_cost_ex(self, from_date: str, to_date: str):
        return self._aggs_cost_ex({k: v["aggs"] for k, v in self._days.items()
                                   if from_date <= k <= to_date})

    def _range_cost(self, from_date: str, to_date: str):
        """旧接口兼容：仅 CNY"""
        cny, _, matched = self._range_cost_ex(from_date, to_date)
        return cny if matched else None

    def _session_cost_ex(self):
        return self._aggs_cost_ex(self._sess["aggs"])

    def _session_cost(self):
        cny, _, matched = self._session_cost_ex()
        return cny if matched else None

    def _channel_cost_ex(self, url: str, name: str):
        """某渠道（URL/渠道名包含匹配）在全部历史里的计费 → (cny, pts, matched)"""
        merged = {}
        for ds in self._days.values():
            for mkey, slots in ds["aggs"].items():
                parts = mkey.split("\u001F")
                channel = parts[1] if len(parts) > 1 else ""
                host = parts[2] if len(parts) > 2 else ""
                if not (_url_loose_match(host, url) or _url_loose_match(channel, url)
                        or (name and name.lower() in channel.lower())):
                    continue
                slots2 = merged.setdefault(mkey, [None, None])
                for i in (0, 1):
                    if slots[i] is None:
                        continue
                    if slots2[i] is None:
                        slots2[i] = {"i": 0, "o": 0, "c": 0}
                    slots2[i]["i"] += slots[i]["i"]
                    slots2[i]["o"] += slots[i]["o"]
                    slots2[i]["c"] += slots[i]["c"]
        if not merged:
            return 0.0, 0.0, False
        cny, pts, matched = self._aggs_cost_ex(merged)
        return cny, pts, matched

    def _channel_cost(self, url: str, name: str) -> float:
        """旧接口兼容：仅 CNY（preset 旧模型扣减用）"""
        cny, _, matched = self._channel_cost_ex(url, name)
        return cny if matched else 0.0

    def _channel_cost_since_ex(self, url: str, name: str, since: datetime):
        """自 since 时刻以来（含）的渠道计费 → (cny, pts, matched)。
        逐条扫日志，按 t >= since 过滤；双币种分开累计。"""
        cny, pts, matched = 0.0, 0.0, False
        for r in self._read_records():
            try:
                t = _parse_ts(r["t"])
            except Exception:
                continue
            if t < since:
                continue
            if not (_url_loose_match(r.get("h", ""), url) or _url_loose_match(r.get("ch", ""), url)
                    or (name and name.lower() in (r.get("ch", "") or "").lower())):
                continue
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            if rule is None:
                continue
            amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t)
            if cur == "积分":
                pts += amt
            else:
                cny += amt
            matched = True
        return cny, pts, matched

    # ── 余额探测 ──

    def _bal_state_of(self, name: str) -> dict:
        s = self._bal_states.get(name) or {"balance": 0, "currency": "CNY", "at": "", "ok": False, "msg": "尚未探测"}
        return s

    def _is_est(self, src: dict) -> bool:
        return (src.get("type") or "auto").strip().lower() in EST_TYPES

    def _src_currency(self, src: dict, fallback: str = "CNY") -> str:
        """源展示币种：显式设置优先；daily/rolling 默认积分；其余默认 CNY"""
        cur = (src.get("currency") or "").strip()
        if cur:
            return cur
        if (src.get("type") or "").strip().lower() in POINT_TYPES:
            return "积分"
        return fallback

    def _resolve_balance_state(self, src: dict) -> dict:
        """当前额度：估算型（preset/daily/rolling）按公式本地推算；
        其余取最近探测结果。统一公式：
          当前 = 设定的「当前余额(对表)」− 自设定时刻以来计费（按价格规则现算，改价即时重估）；
          preset 无锚定时回落 初始额度 − 全历史计费；
          daily 无锚定时 = 每日额度 − 上次刷新以来计费；
          rolling 无锚定时 = 初始额度(基准) − 全历史计费 + 发放次数 × 每日额度"""
        s_type = (src.get("type") or "auto").strip().lower()
        name = src.get("name", "")
        if s_type not in EST_TYPES:
            return self._bal_state_of(name)

        currency = self._src_currency(src)
        anchor = src.get("anchor_balance")
        try:
            anchor = float(anchor) if anchor not in (None, "") else None
        except (TypeError, ValueError):
            anchor = None
        anchor_at = src.get("anchor_at") or ""
        try:
            anchor_dt = _parse_ts(anchor_at) if anchor_at else None
        except Exception:
            anchor_dt = None

        initial = src.get("initial")
        try:
            initial = float(initial) if initial not in (None, "") else None
        except (TypeError, ValueError):
            initial = None
        daily = float(src.get("daily_quota") or 0) if src.get("daily_quota") not in (None, "") else 0.0
        refresh = (src.get("refresh_time") or "00:00").strip() or "00:00"

        now = datetime.now()

        def _fmt(v):
            return f"{v:0.6f}".rstrip("0").rstrip(".")

        try:
            if anchor is not None and anchor_dt is not None:
                # 锚定：设定值 − 其后计费（daily/rolling 另加此后发放；daily 跨刷新自动回落）
                cny, pts, matched = self._channel_cost_since_ex(src.get("url", ""), name, anchor_dt)
                cost = pts if currency == "积分" else cny
                if s_type == "daily" and _last_refresh(now, refresh) > anchor_dt:
                    # 跨刷新：锚定失效，回落每日额度模型
                    cost2 = self._channel_cost_since_ex(src.get("url", ""), name, _last_refresh(now, refresh))
                    c2 = cost2[1] if currency == "积分" else cost2[0]
                    cur = daily - c2
                    return {"balance": cur, "currency": currency,
                            "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                            "msg": f"每日重置：每日额度 {_fmt(daily)} − 本周期计费 {_fmt(c2)} = 当前 {_fmt(cur)}（锚定已跨刷新失效，回落每日额度模型）"}
                if s_type == "rolling":
                    grants = _grant_count(anchor_dt, now, refresh)
                    cur = anchor + grants * daily - cost
                    return {"balance": cur, "currency": currency,
                            "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                            "msg": f"设定余额 {_fmt(anchor)}（{anchor_dt.strftime('%m-%d %H:%M')} 对表）− 其后计费 {_fmt(cost)} + 已发放 {grants} 期 × {_fmt(daily)} = 当前 {_fmt(cur)}（按价格规则估算）"}
                cur = anchor - cost
                return {"balance": cur, "currency": currency,
                        "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                        "msg": f"设定余额 {_fmt(anchor)}（{anchor_dt.strftime('%m-%d %H:%M')} 对表）− 其后计费 {_fmt(cost)} = 当前 {_fmt(cur)}（按价格规则估算）"}

            if s_type == "daily":
                if daily <= 0:
                    return {"balance": 0, "currency": currency, "at": "", "ok": False,
                            "msg": "daily 源需填「每日额度」（当前 = 每日额度 − 本周期计费）；可再填「当前余额(对表)」校准"}
                cny, pts, matched = self._channel_cost_since_ex(src.get("url", ""), name, _last_refresh(now, refresh))
                cost = pts if currency == "积分" else cny
                cur = daily - cost
                return {"balance": cur, "currency": currency,
                        "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                        "msg": f"每日重置：每日额度 {_fmt(daily)} − 上次刷新以来计费 {_fmt(cost)} = 当前 {_fmt(cur)}（刷新 {refresh}）"}

            if s_type == "rolling":
                if anchor is None or anchor_dt is None:
                    return {"balance": 0, "currency": currency, "at": "", "ok": False,
                            "msg": "rolling 源需先填「当前余额(对表)」建立基准（当前 = 设定余额 − 计费 + 每日发放，没用完的结转滚存）"}
                grants = _grant_count(anchor_dt, now, refresh)
                cny, pts, matched = self._channel_cost_ex(src.get("url", ""), name)
                cost = pts if currency == "积分" else cny
                cur = anchor + grants * daily - cost
                return {"balance": cur, "currency": currency,
                        "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                        "msg": f"每日累计：设定余额 {_fmt(anchor)} − 累计计费 {_fmt(cost)} + 发放 {grants} 期 × {_fmt(daily)} = 当前 {_fmt(cur)}（结转滚存，刷新 {refresh}）"}

            # preset
            if initial is None:
                return {"balance": 0, "currency": currency, "at": "", "ok": False,
                        "msg": "preset 源需先填「初始额度」或「当前余额(对表)」"}
            cny, pts, matched = self._channel_cost_ex(src.get("url", ""), name)
            cost = pts if currency == "积分" else cny
            cur = initial - cost
            return {"balance": cur, "currency": currency,
                    "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                    "msg": f"预设扣减：初始额度 {_fmt(initial)} − 累计计费 {_fmt(cost)} = 当前 {_fmt(cur)}（按价格规则估算）"}
        except Exception as e:
            return {"balance": 0, "currency": currency, "at": "", "ok": False, "msg": f"估算失败: {e}"}

    @staticmethod
    def _read_num(json_data, path: str):
        """点路径取数（data.available_balance）；支持数组下标（balance_infos.0.total_balance 或 [0]）"""
        try:
            el = json_data
            if path:
                for raw_seg in path.split("."):
                    seg = raw_seg.strip()
                    if not seg:
                        continue
                    if len(seg) >= 3 and seg[0] == "[" and seg[-1] == "]":
                        seg = seg[1:-1].strip()
                    if seg.isdigit() and isinstance(el, list):
                        idx = int(seg)
                        if idx < 0 or idx >= len(el):
                            return None
                        el = el[idx]
                    elif isinstance(el, dict) and seg in el:
                        el = el[seg]
                    else:
                        return None
            if isinstance(el, (int, float)) and not isinstance(el, bool):
                return float(el)
            if isinstance(el, str):
                try:
                    return float(el)
                except ValueError:
                    return None
            return None
        except Exception:
            return None

    @staticmethod
    def _first_balance_info(data: dict):
        """DeepSeek/智谱风格：balance_infos[0].total_balance (+currency)"""
        currency = ""
        infos = data.get("balance_infos") or []
        if not infos:
            raise ValueError("返回缺少 balance_infos")
        first = infos[0]
        currency = str(first.get("currency", "") or "")
        v = TokenStatsPlugin._read_num(first, "total_balance")
        if v is None:
            raise ValueError("balance_infos[0] 缺少 total_balance")
        return v, currency

    @staticmethod
    def _newapi_extract(data: dict):
        """New-API /api/user/self 风格：多字段自动提取 quota，返回 (quota, 是否找到)"""
        candidates = [
            "quota", "balance", "remaining", "points",
            "totalBalance", "total_balance", "amount", "credit", "available",
        ]
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict):
                for key in candidates:
                    v = TokenStatsPlugin._read_num(inner, key)
                    if v is not None:
                        return v, True
            if "error" in data and isinstance(data.get("error"), dict):
                err = data["error"]
                if isinstance(err, dict) and err.get("message"):
                    raise ValueError(f"NewAPI 返回错误: {err.get('message')}")
            for key in candidates:
                v = TokenStatsPlugin._read_num(data, key)
                if v is not None:
                    return v, True
            # balance_infos 风格兜底
            infos = data.get("balance_infos") or (inner or {}).get("balance_infos") or []
            if infos:
                v = TokenStatsPlugin._read_num(infos[0], "total_balance")
                if v is not None:
                    return v, True
        return None, False

    async def _probe_one(self, src: dict) -> dict:
        """探测单个余额源（auto/custom/newapi），失败返回 Ok=false + 原因"""
        st = {"balance": 0, "currency": self._src_currency(src),
              "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": False, "msg": ""}
        try:
            s_type = (src.get("type") or "auto").strip().lower()
            api_key = src.get("api_key", "") or ""
            jpath = (src.get("json_path") or "").strip()

            if s_type == "newapi":
                # New-API 专属模式：New-Api-User 头 + /api/user/self + quota_conversion 换算
                cu = (src.get("url") or "").strip()
                if not cu:
                    st["msg"] = "newapi 源缺少站点地址"
                    return st
                if not cu.startswith("http"):
                    cu = "https://" + cu
                croot = cu[:-3].rstrip("/") if cu.endswith("/v1") else cu.rstrip("/")
                api_user = str(src.get("api_user", "") or "").strip()
                conversion = src.get("quota_conversion", 500000)
                try:
                    conversion = float(conversion) if conversion not in (None, "") else 500000
                except (TypeError, ValueError):
                    conversion = 500000
                if not api_user:
                    st["msg"] = "newapi 源缺少 api_user（站点后台用户ID）"
                    return st
                try:
                    body = await self._http_get(croot + "/api/user/self", api_key, extra_headers={"New-Api-User": api_user})
                    quota, found = self._newapi_extract(body)
                    if not found:
                        raise ValueError("返回数据中未找到 quota/balance 等字段")
                    st["balance"] = quota / conversion
                    st["ok"] = True
                    st["msg"] = f"New-API 站点：quota {quota:,.0f} ÷ {conversion:g} = {quota / conversion:.4f}"
                    return st
                except Exception as ex:
                    st["msg"] = f"New-API 接口失败: {str(ex)[:160]}"
                    return st

            if s_type == "custom":
                cu = (src.get("url") or "").strip()
                if not cu:
                    st["msg"] = "custom 源缺少接口地址"
                    return st
                if not cu.startswith("http"):
                    cu = "https://" + cu
                croot = cu[:-3].rstrip("/") if cu.endswith("/v1") else cu.rstrip("/")
                cands = [
                    (cu, jpath),
                    (croot + "/user/balance", "balance_infos.0.total_balance"),
                    (croot + "/v1/users/me/balance", "data.available_balance"),
                    (croot + "/v1/user/info", "data.totalBalance"),
                    (croot + "/api/paas/v4/users/me/balance", "balance_infos.0.total_balance"),
                ]
                tried = []
                for ep, path in cands:
                    try:
                        body = await self._http_get(ep, api_key)
                        v = self._read_num(body, path) if path else self._read_num(body, "")
                        if v is None and path == "data.totalBalance":
                            v = self._read_num(body, "data.balance")
                        if v is None:
                            tried.append(ep)
                            continue
                        st["balance"] = v
                        st["ok"] = True
                        st["msg"] = f"自定义接口（{ep}）" + ("，按「余额字段」取数" if jpath else "")
                        return st
                    except Exception as ex:
                        tried.append(f"{ep}（{str(ex)[:60]}）")
                # One-API 系组合兜底
                try:
                    sub = await self._http_get(croot + "/v1/dashboard/billing/subscription", api_key)
                    limit = self._read_num(sub, "hard_limit_usd")
                    if limit is not None:
                        end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                        usage_body = await self._http_get(
                            f"{croot}/v1/dashboard/billing/usage?start_date=2024-01-01&end_date={end}", api_key)
                        used = (self._read_num(usage_body, "total_usage") or 0) / 100
                        st["balance"] = limit - used
                        st["currency"] = "USD"
                        st["ok"] = True
                        st["msg"] = f"One-API 系中转站：额度 {limit:.2f} − 已用 {used:.2f}"
                        return st
                except Exception as ex:
                    tried.append(f"subscription（{str(ex)[:60]}）")
                st["msg"] = "常见余额接口均未取到数字（" + "；".join(tried[-3:])[:240] + "）"
                return st

            # auto：按 URL 分流
            url = (src.get("url") or "").strip()
            if not url:
                st["msg"] = "缺少 URL"
                return st
            if not url.startswith("http"):
                url = "https://" + url
            root = url[:-3].rstrip("/") if url.endswith("/v1") else url.rstrip("/")
            try:
                host = (urlparse(root).hostname or root).lower()
            except Exception:
                host = root.lower()

            if any(h in host for h in _DS_HOSTS):
                body = await self._http_get(root + "/user/balance", api_key)
                v, cur = self._first_balance_info(body)
                st["balance"] = v
                st["currency"] = cur or "CNY"
                st["ok"] = True
                st["msg"] = "DeepSeek 官方端点"
            elif any(h in host for h in _MS_HOSTS):
                body = await self._http_get(root + "/v1/users/me/balance", api_key)
                v = self._read_num(body, "data.available_balance")
                if v is None:
                    raise ValueError("返回缺少 data.available_balance")
                st["balance"] = v
                st["currency"] = "CNY"
                st["ok"] = True
                st["msg"] = "Moonshot 官方端点"
            elif any(h in host for h in _SF_HOSTS):
                body = await self._http_get(root + "/v1/user/info", api_key)
                v = self._read_num(body, "data.totalBalance") or self._read_num(body, "data.balance")
                if v is None:
                    raise ValueError("返回缺少 data.totalBalance/balance")
                st["balance"] = v
                st["currency"] = "CNY"
                st["ok"] = True
                st["msg"] = "硅基流动官方端点"
            elif any(h in host for h in _ZP_HOSTS):
                body = await self._http_get(root + "/api/paas/v4/users/me/balance", api_key)
                v, cur = self._first_balance_info(body)
                st["balance"] = v
                st["currency"] = cur or "CNY"
                st["ok"] = True
                st["msg"] = "智谱端点（官方未文档化，失败可改 custom/preset）"
            else:
                # One-API / New-API 中转站：subscription − usage
                sub = await self._http_get(root + "/v1/dashboard/billing/subscription", api_key)
                limit = self._read_num(sub, "hard_limit_usd")
                if limit is None:
                    raise ValueError("中转站未实现 billing/subscription（hard_limit_usd 缺失）")
                end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                usage_body = await self._http_get(
                    f"{root}/v1/dashboard/billing/usage?start_date=2024-01-01&end_date={end}", api_key)
                used = (self._read_num(usage_body, "total_usage") or 0) / 100
                st["balance"] = limit - used
                st["currency"] = "USD"
                st["ok"] = True
                st["msg"] = f"One-API 系中转站：额度 {limit:.2f} − 已用 {used:.2f}"
        except Exception as ex:
            st["ok"] = False
            st["msg"] = str(ex)[:160]
        return st

    async def _http_get(self, url: str, api_key: str, extra_headers: dict = None):
        """GET 请求，返回解析后的 JSON；非 2xx 抛异常。
        ssl：balance_ssl_verify=True 时校验证书（https 默认行为）；
        False（默认）时禁用校验——兼容自签证书中转站，注意中间人风险"""
        if aiohttp is None:
            raise RuntimeError("aiohttp 未安装")
        headers = {}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        if extra_headers:
            headers.update(extra_headers)
        timeout = aiohttp.ClientTimeout(total=10)
        use_ssl = bool(self.balance_ssl_verify)
        try:
            if use_ssl:
                connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
            else:
                connector = aiohttp.TCPConnector(resolver=ThreadedResolver(), ssl=False)
        except Exception:
            connector = None
        try:
            if connector is not None:
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        body = await resp.text()
                        if resp.status >= 400:
                            raise RuntimeError(f"HTTP {resp.status} {body[:120]}")
                        return json.loads(body)
            else:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        body = await resp.text()
                        if resp.status >= 400:
                            raise RuntimeError(f"HTTP {resp.status} {body[:120]}")
                        return json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError("返回非 JSON")
        except aiohttp.ClientError as e:
            raise RuntimeError(f"网络请求失败: {e}")

    async def _probe_all(self, wait: bool = False):
        """探测全部余额源。

        wait=True：若后台轮询正忙，等待其完成（最多 20s）再返回最新状态，
        避免工具/命令查询拿到旧值；等待超时则返回现有状态不阻塞。
        网络型源并行探测：单源 8s 超时、整体 15s 超时，防止串行 N×10s 拖住查询。
        """
        if self._bal_busy:
            if not wait:
                return
            for _ in range(40):
                if not self._bal_busy:
                    break
                await asyncio.sleep(0.5)
            else:
                logger.warning("[token_stats] 等待余额探测完成超时(20s)，返回现有状态")
            return
        self._bal_busy = True
        try:
            est_srcs = [s for s in self.balance_sources
                        if s.get("enabled", True) and s.get("name") and self._is_est(s)]
            net_srcs = [s for s in self.balance_sources
                        if s.get("enabled", True) and s.get("name") and not self._is_est(s)]
            # 估算型本地推算，不走网络
            for src in est_srcs:
                self._bal_states[src.get("name")] = self._resolve_balance_state(src)

            if net_srcs:
                async def _safe_probe(src):
                    try:
                        st = await asyncio.wait_for(self._probe_one(src), timeout=8)
                    except asyncio.TimeoutError:
                        st = {"balance": 0, "currency": self._src_currency(src),
                              "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              "ok": False, "msg": "探测超时(8s)"}
                    except Exception as e:
                        st = {"balance": 0, "currency": self._src_currency(src),
                              "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              "ok": False, "msg": str(e)[:160]}
                    return src.get("name"), st

                tasks = [asyncio.create_task(_safe_probe(s)) for s in net_srcs]
                done, pending = await asyncio.wait(tasks, timeout=15)
                for t in pending:
                    t.cancel()
                if pending:
                    # 等取消完成，避免事件循环关闭时 Task was destroyed 警告
                    await asyncio.gather(*pending, return_exceptions=True)
                for t in done:
                    try:
                        name, st = t.result()
                        if name:
                            self._bal_states[name] = st
                    except Exception:
                        pass
                if pending:
                    logger.warning(f"[token_stats] 余额并行探测整体超时(15s)，{len(pending)} 个源未完成")
            self._save_bal_states()
        except Exception as e:
            logger.warning(f"[token_stats] 余额探测异常: {e}")
        finally:
            self._bal_busy = False

    async def _balance_loop(self):
        try:
            while True:
                await self._probe_all()
                await asyncio.sleep(max(60, self.balance_interval * 60))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[token_stats] 余额轮询循环退出")

    # ── 后台日志（log.log）ERROR 扫描 ──

    @staticmethod
    def _log_err_classify(text: str) -> str:
        """按内容分类 ERROR 行：xml/model/tool/net/traceback/other"""
        if LOG_ERR_XML_RE.search(text):
            return "xml"
        if LOG_ERR_MODEL_RE.search(text):
            return "model"
        if LOG_ERR_TOOL_RE.search(text):
            return "tool"
        if LOG_ERR_NET_RE.search(text):
            return "net"
        if LOG_ERR_TB_RE.search(text):
            return "traceback"
        return "other"

    def _log_err_scan_file(self, path: Path, pos: int):
        """增量扫描单个日志文件：返回 (新游标, 新增计数, 最新一条)

        二进制模式读取：tell()/seek() 返回真实字节偏移（Python 3.10-3.12 文本模式
        tell() 返回不透明 cookie，不能与 st_size 直接比较，会误判截断）。
        只读打开 + FILE_SHARE_DELETE 共享模式（Windows）：
        不独占文件，不阻塞 KiraAI 的 RotatingFileHandler 轮转 rename。
        句柄生命周期仅限本次扫描（毫秒级），读完立即关闭。
        """
        count = 0
        last = None
        new_pos = pos
        try:
            size = path.stat().st_size
            if size < pos:
                # 文件被轮转/截断：从头扫
                pos = 0
            f = self._open_log_shared(path)
            if f is None:
                return pos, count, last
            try:
                f.seek(pos)
                for raw in f:
                    line = raw.decode("utf-8", errors="replace")
                    m = LOG_ERROR_RE.match(line)
                    if m:
                        count += 1
                        cat = self._log_err_classify(m.group(2))
                        day = line[:10]
                        d = self._log_err_hist.setdefault(day, {})
                        d[cat] = d.get(cat, 0) + 1
                        last = {"at": line[:19], "cat": cat, "text": m.group(2).strip()[:160]}
                new_pos = f.tell()
            finally:
                f.close()
            return new_pos, count, last
        except Exception as e:
            logger.warning(f"[token_stats] 日志扫描失败 {path}: {e}")
            return pos, count, last

    @staticmethod
    def _open_log_shared(path: Path):
        """以共享删除模式打开日志文件（Windows 用 CreateFileW + FILE_SHARE_DELETE，
        避免阻塞 RotatingFileHandler 轮转；其他平台普通只读打开）。二进制模式。"""
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                GENERIC_READ = 0x80000000
                FILE_SHARE_READ = 0x00000001
                FILE_SHARE_WRITE = 0x00000002
                FILE_SHARE_DELETE = 0x00000004
                OPEN_EXISTING = 3
                FILE_ATTRIBUTE_NORMAL = 0x80
                CreateFileW = ctypes.windll.kernel32.CreateFileW
                CreateFileW.restype = wintypes.HANDLE
                CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
                handle = CreateFileW(str(path), GENERIC_READ,
                                     FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                     None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
                if not handle or handle == wintypes.HANDLE(-1).value:
                    return None
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
                if fd < 0:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    return None
                return os.fdopen(fd, "rb")
            return open(path, "rb")
        except Exception:
            return None

    async def _log_err_loop(self):
        """后台循环：每 10s 增量扫描 log.log 及轮转文件（log.log.1/2/…）。

        按文件身份（st_ino）跟踪游标：
        - 轮转改名后同一文件继续从原游标扫，不重复计数
        - 新出现的轮转文件（log.log.1 等）从头扫，历史 ERROR 也能统计到
        - 文件被截断/重建（新 ino）时从头扫
        """
        try:
            while True:
                try:
                    async with self._log_err_lock:
                        self._log_err_scan_all()
                        self._maybe_save_err_stats()
                except Exception as e:
                    logger.warning(f"[token_stats] 日志扫描循环异常: {e}")
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[token_stats] 日志扫描循环退出")

    def _log_err_scan_all(self):
        """扫描 data 目录下全部 log.log* 文件（含轮转），按 ino 增量"""
        try:
            if not (_HAS_DATA_PATH and get_data_path is not None):
                return
            d = Path(get_data_path())
            if not d.is_dir():
                return
            # 显式匹配 log.log + log.log.[0-9]*（RotatingFileHandler 轮转命名），
            # 防未来轮转策略变化误扫无关文件
            files = sorted(
                [p for p in d.glob("log.log") if p.is_file()]
                + [p for p in d.glob("log.log.[0-9]*") if p.is_file()]
            )
            if not files:
                return
            seen = set()
            for path in files:
                try:
                    st = path.stat()
                except OSError:
                    continue
                ino = getattr(st, "st_ino", None)
                if ino is None or ino == 0:
                    # 平台无 ino（如某些网络盘）或 ino 为 0（FAT32 等文件系统）：
                    # 退化为路径跟踪，避免所有文件共用游标 0 导致重复计数
                    ino = str(path)
                seen.add(ino)
                pos = self._log_err_inodes.get(ino, 0)
                size = st.st_size
                if size < pos:
                    # 文件被截断/重建：从头扫
                    pos = 0
                new_pos, count, last = self._log_err_scan_file(path, pos)
                self._log_err_inodes[ino] = new_pos
                if last:
                    self._log_err_last = last
                if count and self.debug_log:
                    logger.info(f"[token_stats] 日志扫描 {path.name} +{count} 条 ERROR")
            # 清理已消失文件的游标（轮转后旧 ino 不再出现）
            for ino in list(self._log_err_inodes):
                if ino not in seen:
                    del self._log_err_inodes[ino]
        except Exception as e:
            logger.warning(f"[token_stats] 日志扫描失败: {e}")

    def _maybe_save_err_stats(self, force: bool = False):
        """错误统计持久化（节流 30s，force 时绕过节流）：热重载/重启不丢"""
        now = time.time()
        if not force and now - self._err_save_at < 30:
            return
        self._err_save_at = now
        try:
            payload = {
                "log_hist": self._log_err_hist,
                "log_last": self._log_err_last,
                "tool_hist": self._tool_err_hist,
                "tool_last": self._tool_err_last,
                # 游标一并持久化：热重载后新实例从原游标续扫，历史 ERROR 不重复计数
                "log_inodes": {str(k): v for k, v in self._log_err_inodes.items()},
            }
            p = self._data_dir / "err_stats.json"
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_err_stats(self):
        """加载持久化的错误统计与扫描游标（热重载/重启恢复）"""
        try:
            p = self._data_dir / "err_stats.json"
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("log_hist"), dict):
                    self._log_err_hist = data["log_hist"]
                if isinstance(data.get("log_last"), dict):
                    self._log_err_last = data["log_last"]
                if isinstance(data.get("tool_hist"), dict):
                    self._tool_err_hist = data["tool_hist"]
                if isinstance(data.get("tool_last"), dict):
                    self._tool_err_last = data["tool_last"]
                if isinstance(data.get("log_inodes"), dict):
                    inodes = {}
                    for k, v in data["log_inodes"].items():
                        try:
                            kk = int(k)
                        except (TypeError, ValueError):
                            kk = k
                        inodes[kk] = v
                    self._log_err_inodes = inodes
        except Exception:
            pass

    def _log_err_summary(self, days: int = 7) -> str:
        """近 N 天分类聚合摘要（含最近一条）"""
        if not self._log_err_hist:
            return ""
        today = datetime.now().strftime("%Y-%m-%d")
        cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        agg = {}
        for day, cats in self._log_err_hist.items():
            if day < cutoff or day > today:
                continue
            for cat, n in cats.items():
                agg[cat] = agg.get(cat, 0) + n
        if not agg:
            return ""
        parts = []
        for cat in LOG_ERR_CATS:
            if agg.get(cat):
                parts.append(f"{LOG_ERR_LABELS[cat]} {agg[cat]}")
        s = "后台日志错误：" + " · ".join(parts)
        if self._log_err_last and self._log_err_last.get("text"):
            s += f"（最近：{self._log_err_last['text'][:60]}）"
        return s

    def _tool_err_summary(self, days: int = 7) -> str:
        """近 N 天工具结果失败摘要（error/权限denied/超时等）"""
        if not self._tool_err_hist:
            return ""
        today = datetime.now().strftime("%Y-%m-%d")
        cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        total = 0
        for day, n in self._tool_err_hist.items():
            if day < cutoff or day > today:
                continue
            total += n
        if not total:
            return ""
        s = f"工具结果失败：{total} 次"
        if self._tool_err_last and self._tool_err_last.get("text"):
            s += f"（最近：{self._tool_err_last['text'][:60]}）"
        return s

    # ── 查询回复构建（命令 / 工具共用）──

    def _fmt_cost_part(self, cny, pts, matched):
        """双币种费用文本：分别展示，永不混算"""
        parts = []
        if matched:
            if cny:
                parts.append(f"费用 ¥{cny:.4f}")
            if pts:
                parts.append(f"积分 {pts:,.4f}")
        return " · ".join(parts)

    def _build_summary_text(self, range_key: str = "") -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        sb = []
        channel, model, _ = self._resolve_channel_model()
        sb.append(f"【Token 用量统计】渠道 {channel} · 模型 {model}")

        def want(k):
            return not range_key or range_key.strip().lower() == k

        if want("session"):
            s = self._sess
            cny, pts, matched = self._session_cost_ex()
            line = f"本次会话：{_fmt_num(s['v'])} tokens · 输入 {_fmt_num(s['i'])} · 输出 {_fmt_num(s['o'])} · 缓存 {_fmt_num(s['c'])} · {s['r']} 轮"
            cp = self._fmt_cost_part(cny, pts, matched)
            if cp:
                line += " · " + cp
            if s["e"] > 0:
                line += f" · 出错 {s['e']}"
            sb.append(line)

        d7 = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        d30 = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
        for key, label, frm, to in (
                ("today", "今天", today, today),
                ("d7", "近7天", d7, today),
                ("d30", "近30天", d30, today),
                ("total", "累计", "0000-01-01", "9999-12-31")):
            if not want(key):
                continue
            agg = self._range_agg(frm, to)
            cny, pts, matched = self._range_cost_ex(frm, to)
            line = f"{label}：{_fmt_num(agg['v'])} tokens · 输入 {_fmt_num(agg['i'])} · 输出 {_fmt_num(agg['o'])} · 缓存 {_fmt_num(agg['c'])} · {agg['r']} 轮"
            cp = self._fmt_cost_part(cny, pts, matched)
            if cp:
                line += " · " + cp
            if agg["e"] > 0:
                line += f" · 出错 {agg['e']}"
            sb.append(line)

        if self.tool_include_balance and self.enable_balance and self.balance_sources:
            try:
                sb.append("账户余额：")
                for src in self.balance_sources:
                    if not src.get("enabled", True):
                        continue
                    st = self._resolve_balance_state(src)
                    name = src.get("name", "")
                    if st["ok"]:
                        # 积分制源显示「积分」，其余用全局余额单位
                        unit = "积分" if self._src_currency(src) == "积分" else self.balance_unit
                        sb.append(f"- {name}：{st['balance']:.4f} {unit}（{st.get('msg', '')[:40]}）")
                    else:
                        sb.append(f"- {name}：探测失败（{st['msg']}）")
            except Exception:
                sb.append("- 余额读取失败")

        if self._last_err_text:
            sb.append(f"最近出错：{self._last_err_text}")
        log_err = self._log_err_summary(7)
        if log_err:
            sb.append(log_err)
        tool_err = self._tool_err_summary(7)
        if tool_err:
            sb.append(tool_err)
        return "\n".join(sb)

    async def _build_query_reply(self, arg: str) -> str:
        arg = arg.strip().lower()
        aliases = {"本次": "session", "今天": "today", "7天": "d7", "近7天": "d7",
                   "30天": "d30", "近30天": "d30", "累计": "total", "余额": "balance"}
        key = aliases.get(arg, arg)
        if key in RANGES:
            text = self._build_summary_text(key)
            return self.cmd_success_template.format(provider=RANGE_LABELS[key], result=text)
        if key == "balance":
            if not self.enable_balance or not self.balance_sources:
                return "未启用余额监测或未配置余额源（插件配置页 → 余额监测）"
            await self._probe_all(wait=True)
            lines = ["💳 账户余额："]
            for src in self.balance_sources:
                if not src.get("enabled", True):
                    continue
                st = self._resolve_balance_state(src)
                name = src.get("name", "")
                if st["ok"]:
                    unit = "积分" if self._src_currency(src) == "积分" else self.balance_unit
                    lines.append(f"- {name}：{st['balance']:.4f} {unit}（{st.get('msg', '')[:40]}）")
                else:
                    lines.append(f"- {name}：探测失败（{st['msg']}）")
            return "\n".join(lines)
        # 默认：全部概览
        return self._build_summary_text("")

    # ── AI 查询函数（维度聚合 / 逐轮明细）──

    def _ai_filter_hit(self, value: str, filter_kw: str) -> bool:
        return not filter_kw or (value or "").lower().find(filter_kw.lower()) >= 0

    def _build_ai_usage(self, dim, range_key, from_date, to_date, model, channel, source, top):
        """维度聚合：dim=channel/model/source/day，支持时间区间与关键字过滤"""
        try:
            d = (dim or "").strip().lower()
            if d not in ("model", "source", "day"):
                d = "channel"
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            def is_iso(s):
                return bool(s and re.match(r"^\d{4}-\d{2}-\d{2}$", s.strip()))

            if is_iso(from_date) and is_iso(to_date):
                f, t = from_date.strip(), to_date.strip()
                if f > t:
                    f, t = t, f
                range_label = f + " ~ " + t
            else:
                rg = range_key if range_key in ("today", "d30", "total") else "d7"
                if rg == "today":
                    f, t, range_label = today, today, "今天"
                elif rg == "d30":
                    f, t, range_label = (now - timedelta(days=29)).strftime("%Y-%m-%d"), today, "近30天"
                elif rg == "total":
                    f, t, range_label = "0000-01-01", "9999-12-31", "累计"
                else:
                    f, t, range_label = (now - timedelta(days=6)).strftime("%Y-%m-%d"), today, "近7天"

            filters = []
            if model:
                filters.append("模型~" + model.strip())
            if channel:
                filters.append("渠道~" + channel.strip())
            if source:
                filters.append("来源~" + source.strip())

            dim_label = {"model": "模型", "source": "来源", "day": "日期"}.get(d, "渠道")
            map_agg = {}
            t_r = t_i = t_o = t_c = t_v = 0
            errs = 0
            cny_tot = pts_tot = 0.0
            any_matched = False

            for r in self._read_records():
                try:
                    t_dt = _parse_ts(r["t"])
                except Exception:
                    continue
                day = t_dt.strftime("%Y-%m-%d")
                if day < f or day > t:
                    continue
                if not (self._ai_filter_hit(r.get("m", ""), model)
                        and self._ai_filter_hit(r.get("ch", ""), channel)
                        and self._ai_filter_hit(r.get("s", ""), source)):
                    continue
                rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
                amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t_dt) if rule else (None, "CNY")
                i, o, c, v = r.get("i", 0), r.get("o", 0), r.get("c", 0), r.get("v", 0)
                key = r.get("m", "") if d == "model" else (r.get("s", "") or "未知") if d == "source" else day if d == "day" else (r.get("ch", "") or "未知")
                if not key:
                    key = "未知"
                a = map_agg.setdefault(key, {"r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "cny": 0.0, "pts": 0.0, "matched": False})
                a["r"] += 1; a["i"] += i; a["o"] += o; a["c"] += c; a["v"] += v
                if amt is not None:
                    if cur == "积分":
                        a["pts"] += amt
                    else:
                        a["cny"] += amt
                    a["matched"] = True
                    if cur == "积分":
                        pts_tot += amt
                    else:
                        cny_tot += amt
                    any_matched = True
                t_r += 1; t_i += i; t_o += o; t_c += c; t_v += v
                errs += max(0, int(r.get("e", 0) or 0))

            sb = [f"【用量·按{dim_label}】{range_label}"]
            if filters:
                sb.append(" · 筛选: " + "、".join(filters))
            sb.append("")
            if not map_agg:
                return _clamp_ai_output("".join(sb) + "（该条件下暂无记录）")
            sb.append(f"{dim_label} | 轮次 | 输入 | 输出 | 缓存 | 合计 | 费用")
            cap = max(1, min(top or 8, 20))
            for k, a in sorted(map_agg.items(), key=lambda x: x[1]["v"], reverse=True)[:cap]:
                cost_txt = "—"
                if a["matched"]:
                    bits = []
                    if a["cny"]:
                        bits.append(f"¥{a['cny']:.4f}")
                    if a["pts"]:
                        bits.append(f"积分 {a['pts']:,.4f}")
                    cost_txt = " + ".join(bits)
                sb.append(f"{k} | {a['r']} | {_fmt_num(a['i'])} | {_fmt_num(a['o'])} | {_fmt_num(a['c'])} | {_fmt_num(a['v'])} | {cost_txt}")
            if len(map_agg) > cap:
                sb.append(f"（共 {len(map_agg)} 行，仅显示前 {cap} 行，可调大 top 或增加过滤条件）")
            tot_bits = []
            if any_matched:
                if cny_tot:
                    tot_bits.append(f"¥{cny_tot:.4f}")
                if pts_tot:
                    tot_bits.append(f"积分 {pts_tot:,.4f}")
            sb.append(f"合计 | {t_r} | {_fmt_num(t_i)} | {_fmt_num(t_o)} | {_fmt_num(t_c)} | {_fmt_num(t_v)} | {' + '.join(tot_bits) if tot_bits else '—'}")
            if errs > 0:
                sb.append(f"出错标记合计：{errs}")
            return _clamp_ai_output("\n".join(sb))
        except Exception as ex:
            return f"用量聚合查询失败：{ex}"

    def _build_ai_records(self, n, model, channel, source, min_input):
        """最近 N 轮逐轮明细，倒序，支持过滤"""
        try:
            want = []
            if model:
                want.append("模型~" + model.strip())
            if channel:
                want.append("渠道~" + channel.strip())
            if source:
                want.append("来源~" + source.strip())
            if min_input is not None:
                want.append(f"输入>{min_input}")
            sb = ["【最近轮次】"]
            if want:
                sb.append(" · 筛选: " + "、".join(want))
            sb.append("")
            sb.append("时间 | 模型 | 来源 | 渠道 | 输入 | 输出 | 缓存 | 合计 | 费用")
            cap = max(1, min(n or 10, 30))
            written = 0
            recs = self._read_records()
            for r in reversed(recs):
                if written >= cap:
                    break
                if not (self._ai_filter_hit(r.get("m", ""), model)
                        and self._ai_filter_hit(r.get("ch", ""), channel)
                        and self._ai_filter_hit(r.get("s", ""), source)):
                    continue
                if min_input is not None and int(r.get("i", 0) or 0) < min_input:
                    continue
                try:
                    t_dt = _parse_ts(r["t"])
                except Exception:
                    t_dt = None
                rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
                amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t_dt) if t_dt and rule else (None, "CNY")
                cost_txt = "—"
                if amt is not None:
                    cost_txt = f"¥{amt:.4f}" if cur != "积分" else f"积分 {amt:,.4f}"
                ts = t_dt.strftime("%m-%d %H:%M:%S") if t_dt else r.get("t", "")
                row = f"{ts} | {r.get('m', '') or '未知'} | {r.get('s', '') or '未知'} | {r.get('ch', '') or '未知'} | " \
                      f"{_fmt_num(r.get('i', 0))} | {_fmt_num(r.get('o', 0))} | {_fmt_num(r.get('c', 0))} | {_fmt_num(r.get('v', 0))} | {cost_txt}"
                e = int(r.get("e", 0) or 0)
                if e > 0:
                    row += f" | 出错{e}"
                sb.append(row)
                written += 1
            if written == 0:
                sb.append("（该条件下暂无记录）")
            else:
                sb.append(f"（匹配 {written} 条）" if written < cap else f"（已达条数上限 {cap}，可调大 n 或增加过滤条件）")
            return _clamp_ai_output("\n".join(sb))
        except Exception as ex:
            return f"轮次明细查询失败：{ex}"
    # ── Bot 工具 ──

    @register.tool(
        name="query_token_stats",
        description="查询 Token 用量统计：本次会话/今天/近7天/近30天/累计的 tokens、轮数、估算费用、出错次数，以及已配置的 API 账户余额。用户问到\"用了多少 token / 花了多少钱 / 余额还剩多少 / 出错了吗\"等时调用。",
        params={
            "type": "object",
            "properties": {
                "range": {
                    "type": "string",
                    "enum": ["", "session", "today", "d7", "d30", "total"],
                    "description": "统计范围：留空返回全部概览，session=本次会话，today=今天，d7=近7天，d30=近30天，total=累计",
                    "default": "",
                }
            },
            "required": [],
        },
    )
    async def query_token_stats(self, event: KiraMessageBatchEvent, range: str = "") -> str:
        if not self.enabled:
            return "Token 统计未启用（插件配置页 → 基础设置）"
        try:
            # 工具查询余额时先即时探测，保证拿到最新值（与 api-balance 插件行为一致）
            if self.tool_include_balance and self.enable_balance and self.balance_sources:
                await self._probe_all(wait=True)
            return self._build_summary_text(range or "")
        except Exception as e:
            logger.exception("[token_stats] tool query failed")
            return f"查询失败：{e}"

    @register.tool(
        name="query_token_usage",
        description="按维度聚合查询历史 Token 用量与费用（可组合过滤条件）。适合\"哪个模型/渠道/来源用得多、某渠道花了多少钱、按天趋势\"类问题；无参数=近7天·按渠道·前8行。",
        params={
            "type": "object",
            "properties": {
                "dim": {
                    "type": "string",
                    "enum": ["channel", "model", "source", "day"],
                    "description": "聚合维度：channel=渠道(默认)/model=模型/source=来源/day=按天",
                    "default": "channel",
                },
                "range": {
                    "type": "string",
                    "enum": ["d7", "today", "d30", "total"],
                    "description": "时间范围：d7=近7天(默认)/today/d30/total；也可用 from+to 指定区间",
                    "default": "d7",
                },
                "from_date": {"type": "string", "description": "起始日期 YYYY-MM-DD（须与 to_date 同用）"},
                "to_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                "model": {"type": "string", "description": "只统计模型名包含此关键字的记录，如 flash"},
                "channel": {"type": "string", "description": "只统计渠道名包含此关键字的记录"},
                "source": {"type": "string", "description": "只统计来源包含此关键字的记录，如 gm/dm"},
                "top": {"type": "integer", "description": "返回行数上限(1-20)，默认8，按Token降序"},
            },
            "required": [],
        },
    )
    async def query_token_usage(self, event: KiraMessageBatchEvent, dim: str = "", range: str = "",
                                from_date: str = "", to_date: str = "", model: str = "", channel: str = "",
                                source: str = "", top: int = 0) -> str:
        if not self.enabled:
            return "Token 统计未启用（插件配置页 → 基础设置）"
        try:
            return self._build_ai_usage(dim, range, from_date, to_date, model, channel, source, top)
        except Exception as e:
            logger.exception("[token_stats] tool usage failed")
            return f"查询失败：{e}"

    @register.tool(
        name="query_token_records",
        description="查询最近N轮对话的逐轮用量明细（时间/模型/来源/渠道/输入/输出/缓存/费用），按时间倒序，可组合过滤；也可用 minInput 查输入超过某 token 数的轮次（定位大上下文）。",
        params={
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "条数(1-30)，默认10"},
                "model": {"type": "string", "description": "只看模型名包含此关键字的轮次"},
                "channel": {"type": "string", "description": "只看渠道名包含此关键字的轮次"},
                "source": {"type": "string", "description": "只看来源包含此关键字的轮次"},
                "minInput": {"type": "integer", "description": "只看输入超过此 token 数的轮次，如 50000 查上下文最大的几轮"},
            },
            "required": [],
        },
    )
    async def query_token_records(self, event: KiraMessageBatchEvent, n: int = 0, model: str = "",
                                  channel: str = "", source: str = "", minInput: int = 0) -> str:
        if not self.enabled:
            return "Token 统计未启用（插件配置页 → 基础设置）"
        try:
            return self._build_ai_records(n or None, model, channel, source, minInput or None)
        except Exception as e:
            logger.exception("[token_stats] tool records failed")
            return f"查询失败：{e}"
    # ── WebUI API（FastAPI 参数注入：query 参数按签名自动解析）──

    @register.api(method="GET", path="/stats", auth=True)
    async def api_stats(self):
        today = datetime.now().strftime("%Y-%m-%d")
        d7 = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        d30 = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
        s = self._sess
        channel, model, _ = self._resolve_channel_model()

        ranges = {
            "session": {"v": s["v"], "i": s["i"], "o": s["o"], "c": s["c"], "r": s["r"], "e": s["e"]},
            "today": self._range_agg(today, today),
            "d7": self._range_agg(d7, today),
            "d30": self._range_agg(d30, today),
            "total": self._range_agg("0000-01-01", "9999-12-31"),
        }
        # 双币种费用：cny / pts 分列，前端按需展示
        def _cost_pair(k):
            cny, pts, matched = k
            return {"cny": f"{cny:.4f}" if matched and cny else None,
                    "pts": f"{pts:,.4f}" if matched and pts else None,
                    "matched": matched}

        costs = {
            "session": _cost_pair(self._session_cost_ex()),
            "today": _cost_pair(self._range_cost_ex(today, today)),
            "d7": _cost_pair(self._range_cost_ex(d7, today)),
            "d30": _cost_pair(self._range_cost_ex(d30, today)),
            "total": _cost_pair(self._range_cost_ex("0000-01-01", "9999-12-31")),
        }
        errors = {
            "session": s.get("e", 0),
            "today": self._range_agg(today, today)["e"],
            "total": self._range_agg("0000-01-01", "9999-12-31")["e"],
            "last": self._last_err_text,
        }
        # 后台日志 ERROR 分类聚合（近7天）
        log_err = {}
        cutoff = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        for day, cats in self._log_err_hist.items():
            if day < cutoff or day > today:
                continue
            for cat, n in cats.items():
                log_err[cat] = log_err.get(cat, 0) + n
        errors["log"] = log_err
        errors["logLast"] = self._log_err_last
        # 工具结果失败（近7天）
        tool_err_total = 0
        for day, n in self._tool_err_hist.items():
            if day < cutoff or day > today:
                continue
            tool_err_total += n
        errors["tool"] = tool_err_total
        errors["toolLast"] = self._tool_err_last
        # 余额摘要：当前渠道匹配的源
        bal_summary = {"sources": len(self.balance_sources), "ok": 0, "current": ""}
        for src in self.balance_sources:
            if not src.get("enabled", True):
                continue
            st = self._resolve_balance_state(src)
            if st["ok"]:
                bal_summary["ok"] += 1
                if not bal_summary["current"]:
                    cur = st.get("currency") or "CNY"
                    unit = "积分" if cur == "积分" else self.balance_unit
                    bal_summary["current"] = f"{src.get('name', '')} {st['balance']:.2f} {unit}"

        return {
            "model": model,
            "channel": channel,
            "src": self._cur_source,
            "elapsed": max(0, int(time.time() - s["start"])),
            "rounds": s["r"],
            "total": s["v"], "input": s["i"], "output": s["o"], "cached": s["c"],
            "lastInput": self._last_round["i"],
            "lastOutput": self._last_round["o"],
            "lastCached": self._last_round["c"],
            "busy": False,
            "logFile": str(self._log_path),
            "ranges": ranges,
            "costs": costs,
            "errors": errors,
            "balance": bal_summary,
        }

    @register.api(method="GET", path="/history", auth=True)
    async def api_history(self, request: Request):
        """/history?day=YYYY-MM-DD → 单天按小时；否则全部按天"""
        day = request.query_params.get("day")
        if day and re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            hours = [{"h": i, **h} for i, h in enumerate(self._hours.get(day, []) or []) if h]
            return {"day": day, "hours": hours}
        days = [{"d": k, "r": v["r"], "v": v["v"], "i": v["i"], "o": v["o"], "c": v["c"], "e": v["e"]}
                for k, v in sorted(self._days.items())]
        return {"days": days}

    @register.api(method="GET", path="/records", auth=True)
    async def api_records(self, request: Request):
        """/records?n=15 → 最近 n 轮（含费用），倒序"""
        try:
            n = max(1, min(100, int(request.query_params.get("n", "15") or 15)))
        except Exception:
            n = 15
        recs = self._read_records()
        out = []
        for r in reversed(recs[-n:]):
            try:
                t = _parse_ts(r["t"])
            except Exception:
                continue
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t) if rule else (None, "CNY")
            out.append({
                "t": t.strftime("%Y-%m-%d %H:%M:%S"),
                "v": r.get("v", 0), "i": r.get("i", 0), "o": r.get("o", 0), "c": r.get("c", 0),
                "m": r.get("m", ""), "s": r.get("s", ""), "ch": r.get("ch", ""),
                "h": r.get("h", ""), "sid": r.get("sid", ""),
                "co": f"{amt:.4f}" if amt is not None else None,
                "cur": cur,
            })
        return {"recs": out}

    @register.api(method="GET", path="/analytics", auth=True)
    async def api_analytics(self, request: Request):
        """/analytics?range=today|d7|d30|total|custom&from=&to="""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        range_key = (request.query_params.get("range") or "today").lower()
        if range_key == "total":
            frm, to = "0000-01-01", "9999-12-31"
        elif range_key == "d7":
            frm, to = (now - timedelta(days=6)).strftime("%Y-%m-%d"), today
        elif range_key == "d30":
            frm, to = (now - timedelta(days=29)).strftime("%Y-%m-%d"), today
        else:
            frm, to = today, today
        frm = request.query_params.get("from") or frm
        to = request.query_params.get("to") or to

        by_source, by_channel, by_model, by_sid = {}, {}, {}, {}
        total = {"r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "cny": 0.0, "pts": 0.0, "matched": False}
        for r in self._read_records():
            try:
                t = _parse_ts(r["t"])
            except Exception:
                continue
            day = t.strftime("%Y-%m-%d")
            if day < frm or day > to:
                continue
            i, o, c, v = r.get("i", 0), r.get("o", 0), r.get("c", 0), r.get("v", 0)
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            amt, cur = _rule_cost_ex(rule, i, o, c, t) if rule else (None, "CNY")

            def add(d, k):
                a = d.setdefault(k, {"r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "cny": 0.0, "pts": 0.0, "matched": False})
                a["r"] += 1; a["i"] += i; a["o"] += o; a["c"] += c; a["v"] += v
                if amt is not None:
                    if cur == "积分":
                        a["pts"] += amt
                    else:
                        a["cny"] += amt
                    a["matched"] = True

            add(by_source, r.get("s", "") or "未知")
            add(by_channel, r.get("ch", "") or "未知")
            add(by_model, r.get("m", "") or "未知")
            add(by_sid, r.get("sid", "") or "未知")
            total["r"] += 1; total["i"] += i; total["o"] += o; total["c"] += c; total["v"] += v
            if amt is not None:
                if cur == "积分":
                    total["pts"] += amt
                else:
                    total["cny"] += amt
                total["matched"] = True

        def dim(d):
            arr = [{"k": k, **v} for k, v in d.items()]
            arr.sort(key=lambda x: x["v"], reverse=True)
            return arr[:20]

        def fmt_cost(a):
            bits = []
            if a.get("matched"):
                if a.get("cny"):
                    bits.append(f"¥{a['cny']:.4f}")
                if a.get("pts"):
                    bits.append(f"积分 {a['pts']:,.4f}")
            return " + ".join(bits) if bits else None

        return {
            "from": frm, "to": to,
            "total": {"r": total["r"], "i": total["i"], "o": total["o"], "c": total["c"], "v": total["v"],
                      "cost": fmt_cost(total)},
            "bySource": dim(by_source),
            "byChannel": dim(by_channel),
            "byModel": dim(by_model),
            "bySid": dim(by_sid),
        }

    @register.api(method="GET", path="/trend", auth=True)
    async def api_trend(self, request: Request):
        """/trend?range=today|d7|d30|total  → 按天时间趋势；
        &day=YYYY-MM-DD → 单天按小时桶（含模型费用分色数据）；
        &day=...&hour=N → 单小时按 5 分钟桶（含模型费用分色数据）。
        每桶 {d, r, v, i, o, c, e, models:[{name,cny,pts}]}；顶层带 Top8 模型 topModels。"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        range_key = (request.query_params.get("range") or "d7").lower()
        day = (request.query_params.get("day") or "").strip()
        hour_s = (request.query_params.get("hour") or "").strip()

        if re.match(r"^\d{4}-\d{2}-\d{2}$", day) and day in self._hours and not hour_s:
            # 单天按小时（不带 hour 参数才走这里；带 hour 下钻到 5 分钟桶）
            hours = []
            for h, hr in enumerate(self._hours[day]):
                if not hr:
                    continue
                hours.append({
                    "d": f"{day} {h:02d}:00", "h": h,
                    "r": hr["r"], "v": hr["v"], "i": hr["i"], "o": hr["o"], "c": hr["c"], "e": hr["e"],
                    "models": self._bucket_fee_models(hr["aggs"]),
                })
            top = self._all_top_models()
            return {"from": day, "to": day, "day": day, "unit": "hour", "days": hours, "topModels": top}

        if day and hour_s.isdigit() and day in self._mins:
            h = int(hour_s)
            if 0 <= h < 24:
                buckets = []
                row = self._mins[day][h]
                if row:
                    for m5, mb in enumerate(row):
                        if not mb:
                            continue
                        buckets.append({
                            "d": f"{day} {h:02d}:{m5 * 5:02d}", "m": m5,
                            "r": mb["r"], "v": mb["v"], "i": mb["i"], "o": mb["o"], "c": mb["c"], "e": mb["e"],
                            "models": self._bucket_fee_models(mb["aggs"]),
                        })
                top = self._all_top_models()
                return {"from": day, "to": day, "day": day, "hour": h, "unit": "min5",
                        "days": buckets, "topModels": top}

        # 按天（原逻辑）
        if range_key == "total" or range_key == "all":
            frm, to = "0000-01-01", "9999-12-31"
        elif range_key == "d30":
            frm, to = (now - timedelta(days=29)).strftime("%Y-%m-%d"), today
        elif range_key == "today":
            frm, to = today, today
        else:
            frm, to = (now - timedelta(days=6)).strftime("%Y-%m-%d"), today

        buckets = {}
        top_models = {}
        for r in self._read_records():
            try:
                t = _parse_ts(r["t"])
            except Exception:
                continue
            d = t.strftime("%Y-%m-%d")
            if d < frm or d > to:
                continue
            i, o, c, v = r.get("i", 0), r.get("o", 0), r.get("c", 0), r.get("v", 0)
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            amt, cur = _rule_cost_ex(rule, i, o, c, t) if rule else (None, "CNY")
            b = buckets.setdefault(d, {"d": d, "r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0})
            b["r"] += 1; b["v"] += v; b["i"] += i; b["o"] += o; b["c"] += c
            b["e"] += max(0, int(r.get("e", 0) or 0))
            mk = r.get("m", "") or "未知"
            tm = top_models.setdefault(mk, {"name": mk, "cny": 0.0, "pts": 0.0})
            if amt is not None:
                if cur == "积分":
                    tm["pts"] += amt
                else:
                    tm["cny"] += amt

        days = [buckets[k] for k in sorted(buckets.keys())]
        models = sorted(top_models.values(), key=lambda x: x["cny"] + x["pts"] / 500, reverse=True)[:8]
        return {"from": frm, "to": to, "unit": "day", "days": days, "topModels": models}

    def _bucket_fee_models(self, aggs: dict):
        """桶内按模型聚合费用（内存 aggs 现算，无磁盘扫描）→ [{name,cny,pts}] Top8"""
        per = {}
        for mkey, slots in (aggs or {}).items():
            parts = mkey.split("\u001F")
            model = parts[0] if len(parts) > 0 else ""
            channel = parts[1] if len(parts) > 1 else ""
            host = parts[2] if len(parts) > 2 else ""
            rule = _match_rule(self.rules, channel, model, host)
            if rule is None:
                continue
            cur = _rule_currency(rule)
            for is_peak, agg in ((False, slots[0]), (True, slots[1])):
                if agg is None:
                    continue
                pk = bool(rule.get("peak_enabled", True)) and is_peak
                hit = rule.get("hit_peak" if pk else "hit_off", 0) or 0
                miss = rule.get("miss_peak" if pk else "miss_off", 0) or 0
                out = rule.get("out_peak" if pk else "out_off", 0) or 0
                amt = (agg["c"] * hit + max(0, agg["i"] - agg["c"]) * miss + agg["o"] * out) / 1_000_000
                m = per.setdefault(model or "未知", {"name": model or "未知", "cny": 0.0, "pts": 0.0})
                if cur == "积分":
                    m["pts"] += amt
                else:
                    m["cny"] += amt
        arr = sorted(per.values(), key=lambda x: x["cny"] + x["pts"] / 500, reverse=True)[:8]
        return [x for x in arr if x["cny"] or x["pts"]]

    def _all_top_models(self):
        """全局 Top8 模型（费用分色图例用）"""
        top = {}
        for r in self._read_records():
            try:
                t = _parse_ts(r["t"])
            except Exception:
                continue
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t) if rule else (None, "CNY")
            if amt is None:
                continue
            mk = r.get("m", "") or "未知"
            m = top.setdefault(mk, {"name": mk, "cny": 0.0, "pts": 0.0})
            if cur == "积分":
                m["pts"] += amt
            else:
                m["cny"] += amt
        return sorted(top.values(), key=lambda x: x["cny"] + x["pts"] / 500, reverse=True)[:8]

    @register.api(method="GET", path="/balance", auth=True)
    async def api_balance(self, request: Request):
        """/balance?refresh=1 → 先强制即时探测再返回；返回完整配置字段（含禁用源），
        展示与可视化编辑器共用同一数据源——编辑回填不丢配置、保存不丢禁用源"""
        if (request.query_params.get("refresh") or "") == "1":
            await self._probe_all(wait=True)
        sources = []
        for src in self.balance_sources:
            if not src.get("enabled", True):
                # 禁用源不探测，只取缓存状态
                st = self._bal_state_of(src.get("name", ""))
            else:
                st = self._resolve_balance_state(src)
            item = {
                "name": src.get("name", ""),
                "type": (src.get("type") or "auto").strip().lower(),
                "enabled": bool(src.get("enabled", True)),
                "est": self._is_est(src),
                "ok": st["ok"],
                "balance": f"{st['balance']:.4f}" if st["ok"] else "",
                "currency": st.get("currency", "CNY"),
                "at": st.get("at", ""),
                "msg": st.get("msg", ""),
            }
            # 配置字段（可视化编辑器回填用，含禁用源）
            for k in ("url", "api_key", "api_user", "json_path", "quota_conversion",
                      "daily_quota", "anchor_balance", "refresh_time", "anchor_at"):
                v = src.get(k)
                if v is not None:
                    item[k] = v
            sources.append(item)
        return {"interval": max(5, self.balance_interval), "unit": self.balance_unit, "sources": sources}

    @register.api(method="POST", path="/balance-config", auth=True)
    async def api_balance_config(self, request: Request):
        """保存余额监测源（WebUI 可视化编辑器）：整体替换 balance_sources 并热重载"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        new_sources = body.get("sources")
        if not isinstance(new_sources, list):
            return {"ok": False, "msg": "sources 必须是数组"}
        # 清洗：只保留合法字段，剔除空项
        cleaned = []
        for s in new_sources:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            if not name:
                continue
            item = {"name": name, "type": (str(s.get("type") or "auto").strip().lower() or "auto"),
                    "enabled": bool(s.get("enabled", True))}
            for k in ("url", "api_key", "api_user", "json_path", "currency"):
                v = s.get(k)
                if v not in (None, ""):
                    item[k] = str(v).strip()
            for k in ("quota_conversion", "daily_quota", "anchor_balance"):
                v = s.get(k)
                if v not in (None, ""):
                    try:
                        item[k] = float(v)
                    except (TypeError, ValueError):
                        pass
            if s.get("refresh_time"):
                item["refresh_time"] = str(s["refresh_time"]).strip()
            if s.get("anchor_at"):
                item["anchor_at"] = str(s["anchor_at"]).strip()
            elif (s.get("type") or "auto").strip().lower() in EST_TYPES and s.get("anchor_balance") not in (None, ""):
                # 仅估算型源：WebUI 表单无 anchor_at 输入，填了「当前余额(对表)」但没给时间时锚定取当前；
                # 编辑已有源时前端会回填原 anchor_at（见 balEditOpen），不会漂移
                item["anchor_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            cleaned.append(item)
        try:
            pm = self.ctx.plugin_mgr
            if pm is None:
                return {"ok": False, "msg": "plugin_mgr 不可用"}
            cfg = pm.get_plugin_config("KiraAI_token_stats_plugin")
            sec = dict(cfg.get("section_balance") or {})
            sec["balance_sources"] = cleaned
            cfg["section_balance"] = sec
            await pm.update_plugin_config("KiraAI_token_stats_plugin", cfg)
            return {"ok": True, "count": len(cleaned)}
        except Exception as e:
            logger.warning(f"[token_stats] 保存余额配置失败: {e}")
            return {"ok": False, "msg": f"保存失败: {e}"}

    @register.api(method="GET", path="/pricing", auth=True)
    async def api_pricing(self):
        return {"rules": self.rules}

    @register.api(method="POST", path="/pricing-config", auth=True)
    async def api_pricing_config(self, request: Request):
        """保存价格规则（WebUI 可视化编辑器）：整体替换 rules 并热重载"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        new_rules = body.get("rules")
        if not isinstance(new_rules, list):
            return {"ok": False, "msg": "rules 必须是数组"}
        # 清洗：只保留合法字段，剔除空项
        cleaned = []
        for r in new_rules:
            if not isinstance(r, dict):
                continue
            name = str(r.get("name") or "").strip()
            if not name:
                continue
            item = {"name": name, "peak_enabled": bool(r.get("peak_enabled", True))}
            for k in ("url_match", "model_match", "channel_match", "currency"):
                v = r.get(k)
                if v not in (None, ""):
                    item[k] = str(v).strip()
            for k in ("hit_peak", "hit_off", "miss_peak", "miss_off", "out_peak", "out_off"):
                v = r.get(k)
                if v not in (None, ""):
                    try:
                        item[k] = float(v)
                    except (TypeError, ValueError):
                        pass
            cleaned.append(item)
        try:
            pm = self.ctx.plugin_mgr
            if pm is None:
                return {"ok": False, "msg": "plugin_mgr 不可用"}
            cfg = pm.get_plugin_config("KiraAI_token_stats_plugin")
            sec = dict(cfg.get("section_pricing") or {})
            sec["rules"] = cleaned
            cfg["section_pricing"] = sec
            await pm.update_plugin_config("KiraAI_token_stats_plugin", cfg)
            # 注意：update_plugin_config 触发热重载，旧实例已被替换，无需（也不能）改 self.rules
            return {"ok": True, "count": len(cleaned)}
        except Exception as e:
            logger.warning(f"[token_stats] 保存价格规则失败: {e}")
            return {"ok": False, "msg": f"保存失败: {e}"}

    # ── WebUI 侧边栏页面 ──

    @register.page("/stats", auth=True, menu=PageMenu(label={"zh": "Token 用量"}, icon="DataLine"))
    def page_stats(self):
        return PluginPage.from_html(_DASHBOARD_HTML)

    @register.page("/stats-widget", auth=True, menu=PageMenu(label={"zh": "Token 挂件"}, icon="Desktop"))
    def page_stats_widget(self):
        if not self.enable_widget:
            return PluginPage.from_html(
                f"<!DOCTYPE html><html lang=\"zh-CN\"><body style=\"background:#0f172a;color:#94a3b8;font-family:sans-serif;padding:24px;font-size:13px\">"
                f"<p style=\"color:#e2e8f0;font-weight:600;margin-bottom:8px\">Token 挂件未启用</p>"
                f"<p>开启方式：插件管理 → KiraAI_token_stats_plugin → 配置 → 「挂件」→ 启用挂件。<br>"
                f"开启后此页面为迷你悬浮卡片（实时 tokens/费用/余额，可拖动、可折叠成小球），适合浏览器小窗钉角落。</p>"
                f"</body></html>")
        # 后端「紧凑模式」配置注入为前端默认值；localStorage 有记忆时以用户为准
        html = _WIDGET_HTML.replace(
            "let compact = localStorage.getItem('tsWidgetCompact')==='1';",
            "let _c = localStorage.getItem('tsWidgetCompact'); let compact = _c === null ? "
            + ("true" if self.widget_compact else "false") + " : _c === '1';",
        )
        return PluginPage.from_html(html)
_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Token 用量统计</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--fg:#e2e8f0;--dim:#94a3b8;--acc:#38bdf8;--ok:#34d399;--warn:#fbbf24;--err:#f87171;--purple:#a78bfa;--pink:#f472b6}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:"Segoe UI",system-ui,"Microsoft YaHei",sans-serif;padding:20px;font-size:14px}
h1{font-size:20px;margin-bottom:4px;display:flex;align-items:center;gap:10px}
h1 .dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.tabs{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
.tab{padding:6px 16px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--dim);cursor:pointer;font-size:13px}
.tab.on{background:var(--acc);color:#06283d;font-weight:600;border-color:var(--acc)}
.panel{display:none}.panel.on{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;position:relative;overflow:hidden}
.card .k{color:var(--dim);font-size:11px;letter-spacing:.5px}
.card .topline{display:flex;justify-content:space-between;align-items:baseline}
.card .v{font-size:22px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}
.card .v.cost{color:var(--ok)}.card .v.in{color:var(--acc)}.card .v.out{color:var(--pink)}.card .v.cache{color:var(--warn)}.card .v.pts{color:#c084fc}
.card .d{color:var(--dim);font-size:11px;margin-top:3px;line-height:1.5}
.card .spark{position:absolute;right:10px;bottom:8px;width:44%;height:26px;opacity:.85}
.card .spark svg{width:100%;height:100%;display:block}
.card .delta{color:var(--dim);font-size:11px}
.card .delta.up{color:var(--ok)}.card .delta.down{color:var(--err)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.box h3{font-size:14px;margin-bottom:10px;color:var(--fg)}
.box h3 .seg{float:right;font-size:11px;color:var(--dim);font-weight:400;cursor:pointer;border:1px solid var(--line);border-radius:999px;padding:2px 10px;margin-left:6px}
.box h3 .seg.on{background:var(--acc);color:#06283d;border-color:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--dim);text-align:left;font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line);font-size:11px;letter-spacing:.5px}
td{padding:6px 8px;border-bottom:1px solid rgba(51,65,85,.4);font-variant-numeric:tabular-nums}
tr:hover td{background:rgba(56,189,248,.05)}
tr.cur td{background:rgba(52,211,153,.07)}
.rate{color:var(--purple)}.ok{color:var(--ok)}.bad{color:var(--err)}
.bar{height:8px;border-radius:4px;background:#0b1220;overflow:hidden;margin-top:4px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--purple));border-radius:4px}
.hours{display:grid;grid-template-columns:repeat(12,1fr);gap:6px}
.hours .h{background:#0b1220;border-radius:6px;padding:6px;text-align:center;font-size:10px;color:var(--dim)}
.hours .h i{display:block;height:46px;background:#0b1220;border-radius:3px;margin:4px 0 2px;position:relative;overflow:hidden}
.hours .h i b{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(180deg,var(--acc),#6366f1);border-radius:3px 3px 0 0}
.hours .h.clickable{cursor:pointer}.hours .h.clickable:hover{border:1px solid var(--acc)}
.btn{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:5px 14px;cursor:pointer;font-size:12px}
.btn:hover{border-color:var(--acc)}
.btn:disabled{opacity:.5;cursor:default}
.btn.on{background:var(--acc);color:#06283d;border-color:var(--acc);font-weight:600}
.snapshot{display:flex;gap:16px;align-items:center;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin-bottom:16px;font-size:12.5px}
.snapshot .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block;margin-right:6px;box-shadow:0 0 6px var(--ok)}
.snapshot .st{color:var(--dim)}
.snapshot b{color:var(--fg)}
.errbox{border-left:3px solid var(--err)}
.note{color:var(--dim);font-size:11.5px;margin-top:8px;line-height:1.6}
/* 时间趋势 */
.trendWrap{position:relative}
.trend{display:flex;align-items:flex-end;gap:3px;height:150px;padding:6px 0 0;position:relative}
.trend .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;position:relative;cursor:pointer;min-width:0}
.trend .col:hover .tip{opacity:1}
.trend .stack{border-radius:2px 2px 0 0;width:100%}
.trend .tlbl{text-align:center;font-size:10px;color:var(--dim);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trend .tip{position:absolute;bottom:100%;left:50%;transform:translateX(-50%);background:#0b1220;border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:11px;white-space:nowrap;opacity:0;transition:.15s;z-index:20;pointer-events:none;color:var(--fg)}
.legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;font-size:11px;color:var(--dim)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.hitline{position:absolute;left:0;right:0;height:1px;background:rgba(167,139,250,.4);pointer-events:none}
</style>
</head>
<body>
<h1><span class="dot" id="dot"></span>Token 用量统计</h1>
<div class="sub" id="sub">加载中…</div>
<div class="tabs">
  <div class="tab on" data-p="ov">概览</div>
  <div class="tab" data-p="trend">时间趋势</div>
  <div class="tab" data-p="dim">维度分析</div>
  <div class="tab" data-p="rec">最近记录</div>
  <div class="tab" data-p="price">价格规则</div>
  <div class="tab" data-p="bal">余额监测</div>
</div>

<div class="panel on" id="p-ov">
  <div class="snapshot" id="snap"></div>
  <div class="cards" id="cards"></div>
  <div class="grid2">
    <div class="box" id="histBox"><h3>按天历史</h3><div id="hist"></div></div>
    <div class="box" id="hourBox"><h3>今日按小时 <span class="note" style="float:right">点击小时柱下钻最近记录</span></h3><div id="hours"></div></div>
  </div>
</div>

<div class="panel" id="p-trend">
  <div class="box">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px">
      <button class="btn on" data-tr="d7">近7天</button>
      <button class="btn" data-tr="d30">近30天</button>
      <button class="btn" data-tr="total">累计</button>
      <span style="color:var(--dim);font-size:12px;margin-left:6px" id="trendRange"></span>
      <span style="flex:1"></span>
      <button class="btn" data-tr="back" id="trendBack" style="display:none">← 返回按天</button>
    </div>
    <div class="trendWrap" id="trendWrap"><div class="trend" id="trend"></div></div>
    <div class="legend" id="trendLegend"></div>
    <div class="note" id="trendNote"></div>
  </div>
</div>

<div class="panel" id="p-dim">
  <div class="box" style="margin-bottom:12px">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
      <button class="btn" data-r="today">今天</button>
      <button class="btn on" data-r="d7">近7天</button>
      <button class="btn" data-r="d30">近30天</button>
      <button class="btn" data-r="total">累计</button>
      <span style="color:var(--dim);font-size:12px" id="dimRange"></span>
    </div>
    <table><thead><tr><th>维度</th><th>值</th><th>轮数</th><th>输入</th><th>输出</th><th>缓存</th><th>总量</th><th>费用</th></tr></thead><tbody id="dimBody"></tbody></table>
  </div>
</div>

<div class="panel" id="p-rec">
  <div class="box">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
      <button class="btn" id="recRefresh">刷新</button>
      <span style="color:var(--dim);font-size:12px">最近 <span id="recCount">15</span> 轮（含工具步），费用按当前价格规则即时计算</span>
    </div>
    <table><thead><tr><th>时间</th><th>模型</th><th>来源</th><th>渠道</th><th>输入</th><th>输出</th><th>缓存</th><th>总量</th><th>费用</th></tr></thead><tbody id="recBody"></tbody></table>
  </div>
</div>

<div class="panel" id="p-price">
  <div class="box">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <button class="btn" id="priceAdd">＋ 添加规则</button>
      <span style="color:var(--dim);font-size:12px" id="priceInfo"></span>
    </div>
    <div id="priceBody"></div>
  </div>
</div>

<div class="panel" id="p-bal">
  <div class="box">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <button class="btn" id="balRefresh">立即探测</button>
      <button class="btn" id="balEdit">＋ 添加监测源</button>
      <span style="color:var(--dim);font-size:12px" id="balInfo"></span>
    </div>
    <table><thead><tr><th>名称</th><th>类型</th><th>余额</th><th>更新时间</th><th>状态</th><th style="width:110px">操作</th></tr></thead><tbody id="balBody"></tbody></table>
    <div class="note">点击「＋ 添加监测源」可视化配置，保存后自动热重载。类型说明：auto=按 URL 自动探测官方端点或 One-API 中转站；custom=自定义接口多端点尝试；newapi=New-API 站点；preset=预设扣减（钱包型）；daily=每日重置积分；rolling=每日累计滚存积分。估算型填「当前余额(对表)」即以上游实际余额校准，此后按价格规则自动扣减。</div>
  </div>
</div>

<div id="balEditor" style="display:none;position:fixed;inset:0;background:rgba(2,6,23,.7);z-index:99;align-items:center;justify-content:center">
  <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;width:560px;max-width:94vw;max-height:88vh;overflow:auto">
    <div style="display:flex;align-items:center;margin-bottom:14px">
      <h3 style="margin:0;flex:1" id="balEditTitle">添加监测源</h3>
      <button class="btn" id="balEditClose">✕</button>
    </div>
    <div id="balEditForm"></div>
  </div>
</div>

<div id="priceEditor" style="display:none;position:fixed;inset:0;background:rgba(2,6,23,.7);z-index:99;align-items:center;justify-content:center">
  <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;width:640px;max-width:94vw;max-height:88vh;overflow:auto">
    <div style="display:flex;align-items:center;margin-bottom:14px">
      <h3 style="margin:0;flex:1" id="priceEditTitle">添加价格规则</h3>
      <button class="btn" id="priceEditClose">✕</button>
    </div>
    <div id="priceEditForm"></div>
  </div>
</div>

<script>
const API = '/api/plugin/KiraAI_token_stats_plugin';
const $ = s => document.querySelector(s);
const fmt = n => Number(n||0).toLocaleString('zh-CN');
const fmt4 = v => { v=Math.max(0,Math.round(v||0)); if(v<1000)return ''+v;
  if(v<9950)return (v/1000).toFixed(1).replace('.0','')+'K'; if(v<995000)return Math.round(v/1000)+'K';
  if(v<9950000)return (v/1e6).toFixed(1).replace('.0','')+'M'; if(v<995000000)return Math.round(v/1e6)+'M';
  return Math.round(v/1e9)+'B'; };
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const localDate = () => { const d=new Date();
  return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); };
const MODEL_COLORS = ['#38bdf8','#a78bfa','#f472b6','#34d399','#fbbf24','#fb7185','#22d3ee','#c084fc'];
const costText = c => c==null ? '—' : (c.indexOf('积分')>=0 ? c : '¥'+c);

document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('on'));
  t.classList.add('on'); $('#p-'+t.dataset.p).classList.add('on');
  if(t.dataset.p==='trend') loadTrend(curTr);
  if(t.dataset.p==='dim') loadDim(curDim);
  if(t.dataset.p==='rec') loadRec();
  if(t.dataset.p==='price') loadPrice();
  if(t.dataset.p==='bal') loadBal(false);
});

async function jget(p){ const r = await fetch(API+p, {cache:'no-store'}); return r.json(); }

function sparkHtml(arr){
  if(!arr || !arr.length) return '';
  const max = Math.max(...arr,1), N = arr.length;
  const pts = arr.map((v,i)=>((i/(N-1||1))*44)+','+ (26-(v/max*24))).join(' ');
  const fill = arr.map((v,i)=>((i/(N-1||1))*44)+','+(26-(v/max*24))).join(' ');
  return '<svg viewBox="0 0 44 26" preserveAspectRatio="none"><polyline points="'+pts+'" fill="none" stroke="rgba(56,189,248,.85)" stroke-width="1.4"/>'+
    '<polygon points="0,26 '+fill+' 44,26" fill="rgba(56,189,248,.13)"/></svg>';
}

const RL = {session:'本次会话',today:'今天',d7:'近7天',d30:'近30天',total:'累计'};
async function loadOv(){
  const d = await jget('/stats');
  $('#dot').style.background = d.busy ? '#60a5fa' : '#34d399';
  $('#sub').textContent = '模型 ' + (d.model||'—') + ' · 渠道 ' + (d.channel||'—') + ' · 日志 ' + (d.logFile||'');
  const el = Math.floor(d.elapsed/60), em = d.elapsed%60;
  $('#snap').innerHTML = '<span><span class="dot"></span>' + esc(d.src||'—') + '</span>' +
    '<span class="st">会话 <b>'+d.rounds+'</b> 轮 · <b>'+fmt4(d.total)+'</b> Token · 已进行 '+(el>0?el+' 分 ':'')+em+' 秒</span>' +
    '<span class="st">最近一轮：输入 <b>'+fmt(d.lastInput)+'</b> · 输出 <b>'+fmt(d.lastOutput)+'</b>'+(d.lastCached?' · 缓存 <b>'+fmt(d.lastCached)+'</b>':'')+'</span>';
  const hist = await jget('/history');
  const dayVols = (hist.days||[]).map(x=>x.v);
  const cards = [];
  for (const k of ['session','today','d7','d30','total']){
    const rg = (d.ranges||{})[k]||{}, co = (d.costs||{})[k]||{};
    const rate = rg.i>0 ? (rg.c/rg.i*100).toFixed(1)+'%' : '—';
    const errs = (d.errors||{})[k]||0;
    const costBits = [];
    if(co.matched){ if(co.cny) costBits.push('¥'+co.cny); if(co.pts) costBits.push('<span class="pts">'+co.pts+' 积分</span>'); }
    // 迷你走势（非本次会话）：近14天分布
    let sp = '';
    if(k!=='session') sp = '<div class="spark">'+sparkHtml(dayVols.slice(-14))+'</div>';
    cards.push('<div class="card"><div class="k">'+RL[k]+'</div><div class="topline"><div class="v">'+fmt4(rg.v)+'</div>'+(sp||'')+'</div>'+
      '<div class="d">'+fmt(rg.r)+' 轮 · 输入 '+fmt(rg.i)+' · 输出 '+fmt(rg.o)+' · 缓存 '+fmt(rg.c)+' · 命中率 <span class="rate">'+rate+'</span></div>'+
      '<div class="d">费用 '+(costBits.length?costBits.join(' + '):'<span class="cost">—</span>')+(errs?' · 出错 <span class="bad">'+errs+'</span>':'')+'</div></div>');
  }
  const er = d.errors||{};
  if (er.last) cards.push('<div class="card errbox"><div class="k">最近出错</div><div class="d">'+esc(er.last)+'</div></div>');
  const lg = er.log||{};
  const lgNames = {xml:'XML解析',model:'模型调用',tool:'工具执行',net:'网络/超时',traceback:'异常堆栈',other:'其他'};
  const lgBits = [];
  for (const c of ['xml','model','tool','net','traceback','other']){
    if (lg[c]) lgBits.push((lgNames[c]||c)+' '+lg[c]);
  }
  if (lgBits.length){
    let lgTxt = '后台日志错误（近7天）：'+lgBits.join(' · ');
    if (er.logLast && er.logLast.text) lgTxt += '｜最近：'+esc(er.logLast.text.slice(0,60));
    cards.push('<div class="card errbox"><div class="k">后台日志 ERROR</div><div class="d">'+lgTxt+'</div></div>');
  }
  if (er.tool){
    let tt = '工具结果失败（近7天）：'+er.tool+' 次';
    if (er.toolLast && er.toolLast.text) tt += '｜最近：'+esc(er.toolLast.text.slice(0,60));
    cards.push('<div class="card errbox"><div class="k">工具结果失败</div><div class="d">'+tt+'</div></div>');
  }
  $('#cards').innerHTML = cards.join('');
  loadHist(hist); loadHours();
}
async function loadHist(hist){
  const d = hist || await jget('/history');
  const days = (d.days||[]).slice(-14);
  if(!days.length){ $('#hist').innerHTML='<div class="note">暂无历史数据</div>'; return; }
  const max = Math.max(...days.map(x=>x.v),1);
  const today = localDate();
  $('#hist').innerHTML = '<table><thead><tr><th>日期</th><th>总量</th><th>轮数</th><th>输入</th><th>输出</th><th>缓存</th><th style="width:30%">分布</th></tr></thead><tbody>'+
    days.map(x=>'<tr'+(x.d===today?' class="cur"':'')+'><td>'+x.d+(x.d===today?' ★':'')+'</td><td>'+fmt4(x.v)+'</td><td>'+x.r+'</td><td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td>'+
    '<td><div class="bar"><i style="width:'+(x.v/max*100)+'%"></i></div></td></tr>').join('')+'</tbody></table>';
}
async function loadHours(){
  const d = await jget('/history?day='+localDate());
  const hs = d.hours||[];
  const max = Math.max(...hs.map(x=>x.v),1);
  const cells = [];
  for(let h=0;h<24;h++){
    const x = hs.find(y=>y.h===h);
    cells.push('<div class="h'+(x?' clickable':'')+'" data-h="'+h+'">'+h+'时<i>'+(x?'<b style="height:'+Math.max(4,x.v/max*100)+'%"></b>':'')+'</i>'+(x?fmt4(x.v):'—')+'</div>');
  }
  $('#hours').innerHTML = '<div class="hours">'+cells.join('')+'</div>';
  document.querySelectorAll('#hours .clickable').forEach(c=>c.onclick=()=>{
    const h = c.dataset.h;
    showTab('rec');
    filterRecByHour(h);
  });
}

/* ── 时间趋势（三级下钻：按天 → 单天按小时 → 单小时按5分钟）── */
let curTr = 'd7', curDay = null, curHour = null;
document.querySelectorAll('[data-tr]').forEach(b=>{
  if(b.id==='trendBack') return;
  b.onclick=()=>{ curDay=null; curHour=null; curTr=b.dataset.tr; document.querySelectorAll('[data-tr]').forEach(x=>{if(x.id!=='trendBack')x.classList.remove('on')}); b.classList.add('on'); loadTrend(curTr); };
});
$('#trendBack').onclick=()=>{
  if(curHour!==null){ curHour=null; loadTrend(); }
  else if(curDay){ curDay=null; loadTrend(); }
};
async function loadTrend(r){
  let url = '/trend?range='+(curDay?curTr:'d7');
  if(curDay && curHour===null) url = '/trend?day='+curDay;   // 后端按天(day=)返回小时桶
  if(curDay && curHour!==null) url = '/trend?day='+curDay+'&hour='+curHour; // 后端按小时(day+hour=)返回5分钟桶
  const d = await jget(url);
  const isDay = !!curDay && curHour===null, isMin = !!curDay && curHour!==null;
  $('#trendRange').textContent = isMin ? ('单小时 '+curDay+' '+String(curHour).padStart(2,'0')+':00 按5分钟（点柱 → 查看该时段记录）')
    : isDay ? ('单天 '+curDay+' 按小时（点柱 → 下钻5分钟/看记录）')
    : (d.from+' ~ '+d.to+'（点柱 → 下钻该天按小时）');
  $('#trendBack').style.display = (curDay||curHour!==null) ? '' : 'none';
  const days = d.days||[], models = d.topModels||[];
  if(!days.length){ $('#trend').innerHTML='<div class="note" style="padding:20px">该范围暂无数据</div>'; $('#trendLegend').innerHTML=''; $('#trendNote').textContent=''; return; }
  const maxV = Math.max(...days.map(x=>x.v),1);
  const legend = [];
  models.forEach((m,i)=>{ if(m.cny||m.pts) legend.push('<span><i style="background:'+MODEL_COLORS[i%8]+'"></i>'+esc(m.name)+'</span>'); });
  legend.push('<span><i style="background:#334155"></i>未计价</span>');
  $('#trendLegend').innerHTML = legend.join('');
  $('#trendNote').textContent = '柱高=总量，堆叠色=Top8 模型费用分色；紫色虚线=缓存命中率（右轴 0-100%）。'+(isMin?'当前为单小时按5分钟桶。':(isDay?'当前为单天按小时桶。':''))+'点柱：'+(isMin?'→ 查看该时段记录':(isDay?'→ 下钻该小时按5分钟':'→ 下钻该天按小时'))+'；「← 返回」逐级回退。';
  const rows = days.map(x=>{
    const stack = [];
    const bm = x.models||[];
    if(bm.length){
      const total = bm.reduce((s,m)=>s+m.cny+(m.pts||0)/500,0);
      if(total>0){
        bm.forEach((m,i)=>{
          const w = (m.cny+(m.pts||0)/500)/total*100;
          if(w<0.3) return;
          stack.push('<div class="stack" style="height:'+w+'%;background:'+MODEL_COLORS[i%8]+'" title="'+esc(m.name)+'"></div>');
        });
      }
    }
    if(!stack.length) stack.push('<div class="stack" style="height:100%;background:#334155"></div>');
    const rate = x.i>0 ? (x.c/x.i*100) : 0;
    const costLine = bm.length ? bm.slice(0,3).map(m=>esc(m.name)+' ¥'+m.cny.toFixed(2)).join(' · ') : '';
    const tip = '<div class="tip">'+(x.d)+(x.h!=null?' 时':'')+(x.m!=null?':'+String(x.m*5).padStart(2,'0'):'')+'<br>'+(x.r)+' 轮 · '+fmt4(x.v)+' Token<br>输入 '+fmt(x.i)+' · 输出 '+fmt(x.o)+' · 缓存 '+fmt(x.c)+'<br>命中率 <span class="rate">'+rate.toFixed(1)+'%</span>'+(x.e?' · 出错 '+x.e:'')+(costLine?'<br>'+costLine:'')+'</div>';
    return '<div class="col" data-d="'+esc(x.d)+'" data-h="'+(x.h!=null?x.h:'')+'" data-m="'+(x.m!=null?x.m:'')+'">'+tip+'<div style="display:flex;flex-direction:column;justify-content:flex-end;height:100%">'+
      (stack.join(''))+'</div>'+
      '<div class="tlbl">'+esc(x.d.length>10?x.d.slice(x.d.length-5):x.d)+'</div></div>';
  }).join('');
  $('#trend').innerHTML = rows;
  document.querySelectorAll('#trend .col').forEach(c=>c.onclick=()=>{
    const d = c.dataset.d, h = c.dataset.h, m = c.dataset.m;
    if(!curDay){ curDay = d.slice(0,10); loadTrend(); }
    else if(curDay && curHour===null && h!==''){ curHour = parseInt(h,10); loadTrend(); }
    else { // 5分钟桶 → 跳记录并过滤该时段
      showTab('rec');
      filterRecBySlot(d.slice(0,10), parseInt(h,10), m===''?null:parseInt(m,10));
    }
  });
}

let recSlotFilter = null; // {day, h, m5|null}
function filterRecByHour(h){ recSlotFilter = {day:null, h:h, m5:null}; $('#recCount').textContent = h+'时'; loadRec(); }
function filterRecBySlot(day, h, m5){ recSlotFilter = {day:day, h:h, m5:m5}; $('#recCount').textContent = (m5!=null? (h+':'+String(m5*5).padStart(2,'0')) : (h+'时')); loadRec(); }
function showTab(name){
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('on'));
  document.querySelector('.tab[data-p="'+name+'"]').classList.add('on');
  $('#p-'+name).classList.add('on');
}

let curDim='d7';
document.querySelectorAll('[data-r]').forEach(b=>b.onclick=()=>{curDim=b.dataset.r;document.querySelectorAll('[data-r]').forEach(x=>x.classList.remove('on'));b.classList.add('on');loadDim(curDim)});
async function loadDim(r){
  const d = await jget('/analytics?range='+r);
  $('#dimRange').textContent = d.from + ' ~ ' + d.to + '（' + RL[r] + '）';
  const rows = [];
  const push = (name, arr) => (arr||[]).forEach(x=>{
    rows.push('<tr><td>'+esc(name)+'</td><td>'+esc(x.k)+'</td><td>'+x.r+'</td><td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td><td>'+fmt(x.v)+'</td><td>'+(x.cost||'—')+'</td></tr>');
  });
  push('来源', d.bySource); push('渠道', d.byChannel); push('模型', d.byModel); push('会话', d.bySid);
  $('#dimBody').innerHTML = rows.join('') || '<tr><td colspan="8" class="note">该范围暂无数据</td></tr>';
}
async function loadRec(){
  const d = await jget('/records?n='+(recSlotFilter?100:15));
  let recs = d.recs||[];
  if(recSlotFilter){
    const sf = recSlotFilter;
    recs = recs.filter(x=>{
      const mt = x.t.match(/^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})/);
      if(!mt) return false;
      if(sf.day && mt[1]!==sf.day) return false;
      if(parseInt(mt[2],10)!==sf.h) return false;
      if(sf.m5!=null && Math.floor(parseInt(mt[3],10)/5)!==sf.m5) return false;
      return true;
    });
  }
  $('#recBody').innerHTML = recs.map(x=>
    '<tr><td>'+esc(x.t)+'</td><td>'+esc(x.m)+'</td><td>'+esc(x.s)+'</td><td>'+esc(x.ch)+'</td>'+
    '<td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td><td>'+fmt(x.v)+'</td><td>'+(x.co?(x.cur==='积分'?x.co+' 积分':'¥'+x.co):'—')+'</td></tr>').join('') ||
    '<tr><td colspan="9" class="note">'+(recSlotFilter?'该时段暂无记录':'暂无记录')+'</td></tr>';
  recSlotFilter = null;
}
let priceRules = [];
let priceEditIdx = -1;
async function loadPrice(){
  const d = await jget('/pricing');
  priceRules = d.rules||[];
  $('#priceInfo').textContent = '共 ' + priceRules.length + ' 条规则';
  $('#priceBody').innerHTML = priceRules.length ? '<table><thead><tr><th>名称</th><th>币种</th><th>URL 匹配</th><th>模型匹配</th><th>渠道匹配</th><th>峰谷</th><th>缓存命中(峰/谷)</th><th>未命中(峰/谷)</th><th>输出(峰/谷)</th><th style="width:110px">操作</th></tr></thead><tbody>'+
    priceRules.map((r,i)=>'<tr><td>'+esc(r.name||'')+'</td><td>'+(r.currency==='积分'?'积分':'¥元')+'</td><td>'+esc(r.url_match||'')+'</td><td>'+esc(r.model_match||'')+'</td><td>'+esc(r.channel_match||'')+'</td>'+
    '<td>'+(r.peak_enabled!==false?'峰谷':'恒谷')+'</td><td>'+r.hit_peak+' / '+r.hit_off+'</td><td>'+r.miss_peak+' / '+r.miss_off+'</td><td>'+r.out_peak+' / '+r.out_off+'</td>'+
    '<td><button class="btn" onclick="priceEditOpen('+i+')">编辑</button> <button class="btn" onclick="priceDel('+i+')">删除</button></td></tr>').join('')+'</tbody></table>'+
    '<div class="note">匹配加权 URL=4 分、模型=2 分、渠道名=1 分取最高；价格单位 元（或积分）/百万 tokens；峰=工作日 9:00-12:00、14:00-18:00。双币种分别累计，不与 ¥ 混算。改价后全历史费用即时重算。</div>'
    : '<div class="note">暂无价格规则，费用显示「—」。点「＋ 添加规则」配置第一条。</div>';
}
const PRICE_FIELDS = [
  ['name','规则名称','如 DeepSeek V4-Flash（官方价）'],
  ['url_match','URL 匹配（可选）','api.deepseek.com'],
  ['model_match','模型匹配（可选）','flash'],
  ['channel_match','渠道匹配（可选）','deepseek'],
  ['currency','币种','CNY（或填 积分）'],
  ['hit_peak','缓存命中·峰时价','0.10'],
  ['hit_off','缓存命中·谷时价','0.05'],
  ['miss_peak','未命中·峰时价','3.0'],
  ['miss_off','未命中·谷时价','1.5'],
  ['out_peak','输出·峰时价','9.0'],
  ['out_off','输出·谷时价','4.5']
];
function priceFieldHtml(f){
  return '<div style="margin-bottom:10px"><label style="display:block;font-size:12px;color:var(--dim);margin-bottom:4px">'+esc(f[1])+'</label>'+
    '<input id="pf_'+f[0]+'" style="width:100%;box-sizing:border-box;background:#0b1220;border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font-size:13px" placeholder="'+esc(f[2])+'"></div>';
}
function priceEditOpen(i){
  priceEditIdx = i;
  const r = (i>=0 && priceRules[i]) ? priceRules[i] : {name:'',currency:'CNY',peak_enabled:true};
  const pe = r.peak_enabled !== undefined ? r.peak_enabled : true; // 旧规则无该字段按启用处理
  $('#priceEditTitle').textContent = i>=0 ? '编辑价格规则' : '添加价格规则';
  let html = priceFieldHtml(['name','规则名称',r.name||'']);
  html += '<div style="margin-bottom:10px"><label style="display:block;font-size:12px;color:var(--dim);margin-bottom:4px">峰谷价</label><select id="pf_peak_enabled" style="width:100%;background:#0b1220;border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font-size:13px">'+
    '<option value="1"'+(pe?' selected':'')+'>启用（工作日 9-12 / 14-18 为峰，其余谷）</option>'+
    '<option value="0"'+(pe?'':' selected')+'>不启用（全天按谷价）</option></select></div>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">';
  PRICE_FIELDS.slice(1).forEach(f=>{ html += priceFieldHtml(f); });
  html += '</div>';
  html += '<div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn" id="pf_cancel">取消</button><button class="btn on" id="pf_save">保存</button></div>';
  $('#priceEditForm').innerHTML = html;
  $('#pf_name').value = r.name||'';
  PRICE_FIELDS.slice(1).forEach(f=>{
    const v = r[f[0]];
    if(v!=null && v!=='') $('#pf_'+f[0]).value = v;
  });
  $('#pf_cancel').onclick = ()=>{ $('#priceEditor').style.display='none'; };
  $('#pf_save').onclick = async ()=>{
    const item = {name:$('#pf_name').value.trim(), peak_enabled:$('#pf_peak_enabled').value==='1'};
    if(!item.name){ alert('请填写规则名称'); return; }
    PRICE_FIELDS.slice(1).forEach(f=>{
      const el = $('#pf_'+f[0]);
      if(!el || el.value.trim()==='') return;
      const v = el.value.trim();
      if(f[0]==='currency') item.currency = v;
      else if(f[0]==='url_match'||f[0]==='model_match'||f[0]==='channel_match') item[f[0]] = v;
      else { const n = parseFloat(v); if(!isNaN(n)) item[f[0]] = n; }
    });
    if(priceEditIdx>=0 && priceRules[priceEditIdx]) priceRules[priceEditIdx] = item;
    else priceRules.push(item);
    const r2 = await fetch(API+'/pricing-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rules:priceRules})}).then(x=>x.json());
    if(r2.ok){ $('#priceEditor').style.display='none'; loadPrice(); loadOv(); }
    else alert('保存失败：'+(r2.msg||'未知错误'));
  };
  $('#priceEditor').style.display='flex';
}
async function priceDel(i){
  if(!confirm('删除价格规则「'+(priceRules[i]?priceRules[i].name:'')+'」？')) return;
  priceRules.splice(i,1);
  const r = await fetch(API+'/pricing-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rules:priceRules})}).then(x=>x.json());
  if(r.ok){ loadPrice(); loadOv(); }
  else alert('删除失败：'+(r.msg||'未知错误'));
}
$('#priceAdd').onclick = ()=>priceEditOpen(-1);
$('#priceEditClose').onclick = ()=>{ $('#priceEditor').style.display='none'; };
let balSources = [];
let balEditIdx = -1;
async function loadBal(refresh){
  const d = await jget('/balance'+(refresh?'?refresh=1':''));
  balSources = d.sources||[];
  $('#balInfo').textContent = '轮询间隔 ' + d.interval + ' 分钟';
  $('#balBody').innerHTML = balSources.map((x,i)=>{
    const unit = x.currency==='积分' ? '积分' : (d.unit||'元');
    return '<tr'+(x.enabled===false?' style="opacity:.55"':'')+'><td>'+esc(x.name)+(x.enabled===false?' <span class="note">(禁用)</span>':'')+'</td><td>'+esc(x.type)+(x.est?' <span class="note">(估算)</span>':'')+'</td><td class="'+(x.ok?'ok':'bad')+'">'+(x.ok?(x.balance+' '+esc(unit)):'失败')+'</td>'+
    '<td>'+esc(x.at||'—')+'</td><td class="note">'+esc(x.msg||'—')+'</td>'+
    '<td><button class="btn" onclick="balEditOpen('+i+')">编辑</button> <button class="btn" onclick="balDel('+i+')">删除</button></td></tr>';
  }).join('') ||
    '<tr><td colspan="6" class="note">未配置余额监测源，点「＋ 添加监测源」开始</td></tr>';
}
const BAL_TYPES = [
  ['auto','auto · 自动探测（官方端点/One-API 中转站）'],
  ['custom','custom · 自定义接口'],
  ['newapi','newapi · New-API 站点'],
  ['preset','preset · 预设扣减（钱包型）'],
  ['daily','daily · 每日重置积分'],
  ['rolling','rolling · 每日累计滚存积分']
];
const BAL_FIELDS = {
  auto: [['url','站点/接口地址','https://api.deepseek.com'],['api_key','API Key','']],
  custom: [['url','接口地址','https://myproxy.example.com'],['api_key','API Key',''],['json_path','余额字段路径(可选)','balance_infos.0.total_balance']],
  newapi: [['url','站点地址','https://newapi.example.com'],['api_key','系统访问令牌',''],['api_user','用户ID(纯数字)',''],['quota_conversion','换算比例(默认500000)','500000']],
  preset: [['anchor_balance','当前余额(对表)',''],['currency','币种(CNY/积分)','CNY']],
  daily: [['daily_quota','每日额度','1000'],['refresh_time','刷新时刻 HH:mm','00:00'],['anchor_balance','当前余额(对表,可选)',''],['currency','币种','积分']],
  rolling: [['daily_quota','每日额度','1000'],['refresh_time','刷新时刻 HH:mm','00:00'],['anchor_balance','当前余额(对表)',''],['currency','币种','积分']]
};
function balFieldHtml(f){
  return '<div style="margin-bottom:10px"><label style="display:block;font-size:12px;color:var(--dim);margin-bottom:4px">'+esc(f[1])+'</label>'+
    '<input id="bf_'+f[0]+'" style="width:100%;box-sizing:border-box;background:#0b1220;border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font-size:13px" placeholder="'+esc(f[2])+'"></div>';
}
function balEditOpen(i){
  balEditIdx = i;
  const src = (i>=0 && balSources[i]) ? balSources[i] : {name:'',type:'auto',enabled:true};
  $('#balEditTitle').textContent = i>=0 ? '编辑监测源' : '添加监测源';
  let html = balFieldHtml(['name','名称',src.name||'']);
  html += '<div style="margin-bottom:10px"><label style="display:block;font-size:12px;color:var(--dim);margin-bottom:4px">类型</label><select id="bf_type" style="width:100%;background:#0b1220;border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font-size:13px">'+
    BAL_TYPES.map(t=>'<option value="'+t[0]+'"'+(src.type===t[0]?' selected':'')+'>'+esc(t[1])+'</option>').join('')+'</select></div>';
  html += '<div id="bf_dyn"></div>';
  html += '<div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim)"><input type="checkbox" id="bf_enabled"'+(src.enabled===false?'':' checked')+'> 启用</label></div>';
  html += '<div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn" id="bf_cancel">取消</button><button class="btn on" id="bf_save">保存</button></div>';
  $('#balEditForm').innerHTML = html;
  $('#bf_name').value = src.name||'';
  const renderDyn = ()=>{
    const t = $('#bf_type').value;
    $('#bf_dyn').innerHTML = (BAL_FIELDS[t]||[]).map(f=>balFieldHtml(f)).join('');
    (BAL_FIELDS[t]||[]).forEach(f=>{
      const v = src[f[0]];
      if(v!=null && v!=='') $('#bf_'+f[0]).value = v;
    });
  };
  renderDyn();
  // 编辑已有源：回填 anchor_at（表单无输入框，用隐藏字段原样保留，避免锚定基准漂移）
  if(i>=0 && src.anchor_at) $('#bf_dyn').insertAdjacentHTML('beforeend', '<input type="hidden" id="bf_anchor_at" value="'+esc(src.anchor_at)+'">');
  $('#bf_type').onchange = renderDyn;
  $('#bf_cancel').onclick = ()=>{ $('#balEditor').style.display='none'; };
  $('#bf_save').onclick = async ()=>{
    const t = $('#bf_type').value;
    const item = {name:$('#bf_name').value.trim(), type:t, enabled:$('#bf_enabled').checked};
    if(!item.name){ alert('请填写名称'); return; }
    (BAL_FIELDS[t]||[]).forEach(f=>{
      const el = $('#bf_'+f[0]);
      if(el && el.value.trim()!=='') item[f[0]] = el.value.trim();
    });
    const ha = $('#bf_anchor_at');
    if(ha && ha.value) item.anchor_at = ha.value;
    if(balEditIdx>=0 && balSources[balEditIdx]) balSources[balEditIdx] = item;
    else balSources.push(item);
    const r = await fetch(API+'/balance-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({sources:balSources})}).then(x=>x.json());
    if(r.ok){ $('#balEditor').style.display='none'; loadBal(false); }
    else alert('保存失败：'+(r.msg||'未知错误'));
  };
  $('#balEditor').style.display='flex';
}
async function balDel(i){
  if(!confirm('删除监测源「'+(balSources[i]?balSources[i].name:'')+'」？')) return;
  balSources.splice(i,1);
  const r = await fetch(API+'/balance-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({sources:balSources})}).then(x=>x.json());
  if(r.ok) loadBal(false);
  else alert('删除失败：'+(r.msg||'未知错误'));
}
$('#balRefresh').onclick = ()=>{ $('#balRefresh').disabled=true; loadBal(true).finally(()=>$('#balRefresh').disabled=false); };
$('#balEdit').onclick = ()=>balEditOpen(-1);
$('#balEditClose').onclick = ()=>{ $('#balEditor').style.display='none'; };
$('#recRefresh').onclick = ()=>{ recSlotFilter=null; $('#recCount').textContent='15'; loadRec(); };

loadOv();
setInterval(loadOv, 5000);
</script>
</body>
</html>
"""


_WIDGET_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Token 挂件</title>
<style>
:root{--bg:rgba(15,23,42,.92);--card:#1e293b;--line:#334155;--fg:#e2e8f0;--dim:#94a3b8;--acc:#38bdf8;--ok:#34d399;--warn:#fbbf24;--pink:#f472b6;--purple:#a78bfa}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{background:transparent;font-family:"Segoe UI",system-ui,"Microsoft YaHei",sans-serif;overflow:hidden}
#w{position:fixed;left:16px;top:16px;width:300px;background:var(--bg);border:1px solid var(--line);border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.45);backdrop-filter:blur(8px);user-select:none;z-index:9999}
#w.dragging{opacity:.85;border-color:var(--acc)}
.head{display:flex;align-items:center;gap:8px;padding:10px 12px;cursor:move;border-bottom:1px solid rgba(51,65,85,.5)}
.head .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 6px var(--ok);flex:none}
.head .t{font-size:12px;font-weight:600;color:var(--fg);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.head .hbtn{width:22px;height:22px;border:none;background:rgba(51,65,85,.4);color:var(--dim);border-radius:6px;cursor:pointer;font-size:12px;line-height:1;flex:none}
.head .hbtn:hover{color:var(--fg);background:var(--line)}
.body{padding:10px 12px 12px}
.row{display:flex;align-items:center;justify-content:space-between;padding:4px 0;font-size:12px}
.row .k{color:var(--dim)}
.row .v{font-variant-numeric:tabular-nums;font-weight:600;color:var(--fg)}
.row .v.cost{color:var(--ok)}.row .v.pts{color:var(--purple)}.row .v.in{color:var(--acc)}.row .v.out{color:var(--pink)}
.sep{height:1px;background:rgba(51,65,85,.5);margin:6px 0}
.bal{font-size:11.5px}
.bal .bname{color:var(--dim);max-width:120px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.foot{padding:8px 12px;border-top:1px solid rgba(51,65,85,.5);display:flex;gap:6px;align-items:center;justify-content:flex-end}
.foot .st{font-size:10px;color:var(--dim);margin-right:auto}
.foot .go{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:3px 10px;cursor:pointer;font-size:11px;text-decoration:none}
.foot .go:hover{border-color:var(--acc)}
/* 折叠小球 */
#w.ball{width:auto;border-radius:999px;overflow:hidden}
#w.ball .head{border-bottom:none;padding:8px 14px}
#w.ball .body,#w.ball .foot{display:none}
#w.ball .t{font-size:16px}
/* 紧凑模式 */
#w.compact{width:230px}
#w.compact .row{font-size:11px;padding:2px 0}
#w.compact .bal .bname{max-width:80px}
/* 独立窗口（popup）模式：卡片填满窗口 */
body.popup{background:#0f172a}
body.popup #w{left:0;top:0;width:100%;height:100%;border:none;border-radius:0;box-shadow:none;display:flex;flex-direction:column}
body.popup .body{flex:1;overflow:auto}
body.popup .head{cursor:default}
/* 提示条 */
#toast{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);background:#0b1220;border:1px solid var(--line);color:var(--fg);font-size:11px;padding:6px 12px;border-radius:8px;opacity:0;transition:.2s;pointer-events:none;z-index:10000;white-space:nowrap}
#toast.show{opacity:1}
</style>
</head>
<body>
<div id="w">
  <div class="head" id="hd">
    <span class="dot" id="dot"></span>
    <span class="t" id="title">Token 用量</span>
    <button class="hbtn" id="popout" title="弹出独立窗口">⧉</button>
    <button class="hbtn" id="fold" title="折叠/展开">–</button>
    <button class="hbtn" id="close" title="关闭窗口" style="display:none">✕</button>
  </div>
  <div class="body" id="body">
    <div class="row"><span class="k">会话</span><span class="v" id="sessV">—</span></div>
    <div class="row"><span class="k">今日</span><span class="v" id="todayV">—</span></div>
    <div class="row"><span class="k">费用(今日)</span><span class="v cost" id="costV">—</span></div>
    <div class="row"><span class="k">输入 / 输出</span><span class="v" id="ioV">—</span></div>
    <div class="row"><span class="k">模型</span><span class="v" id="modelV" style="font-size:11px;color:var(--dim)">—</span></div>
    <div class="sep"></div>
    <div id="balBox"></div>
  </div>
  <div class="foot" id="foot">
    <span class="st" id="upd">—</span>
    <a class="go" href="./stats" target="_blank" rel="noopener">完整看板</a>
    <a class="go" href="stats-widget" target="_blank" rel="noopener" id="tabLink">新标签</a>
  </div>
</div>
<div id="toast"></div>
<script>
const API = '/api/plugin/KiraAI_token_stats_plugin';
const $ = s => document.querySelector(s);
const fmt4 = v => { v=Math.max(0,Math.round(v||0)); if(v<1000)return ''+v;
  if(v<9950)return (v/1000).toFixed(1).replace('.0','')+'K'; if(v<995000)return Math.round(v/1000)+'K';
  if(v<9950000)return (v/1e6).toFixed(1).replace('.0','')+'M'; if(v<995000000)return Math.round(v/1e6)+'M';
  return Math.round(v/1e9)+'B'; };
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const isPopup = !!window.opener;
let collapsed = localStorage.getItem('tsWidgetCollapsed')==='1';
let compact = localStorage.getItem('tsWidgetCompact')==='1';
let toastTimer = null;
function toast(msg){
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(()=>t.classList.remove('show'), 2600);
}
applyMode();
$('#fold').onclick = ()=>{ collapsed=!collapsed; localStorage.setItem('tsWidgetCollapsed', collapsed?'1':'0'); applyMode(); };
$('#popout').onclick = ()=>{
  const w = window.open(location.pathname, 'tsWidgetPopup', 'popup=yes,width=360,height=500,resizable=yes');
  if(!w) toast('弹窗被拦截：请允许本站弹窗，或用底部「新标签」打开');
};
$('#close').onclick = ()=>{ try{ window.close(); }catch(e){ toast('无法自动关闭，请手动关闭窗口'); } };
function applyMode(){
  const w = $('#w');
  w.classList.toggle('ball', collapsed);
  w.classList.toggle('compact', !collapsed && compact);
  $('#fold').textContent = collapsed ? '+' : '–';
  $('#title').textContent = collapsed ? '☰' : 'Token 用量';
}
if(isPopup){
  document.body.classList.add('popup');
  $('#close').style.display = '';
  $('#popout').style.display = 'none';
  $('#fold').style.display = 'none';
  $('#tabLink').style.display = 'none';
}
async function tick(){
  try{
    const d = await fetch(API+'/stats',{cache:'no-store'}).then(r=>r.json());
    const r = (d.ranges||{})||{}, co = (d.costs||{})||{};
    const t = co.today||{};
    $('#dot').style.background = d.busy ? '#60a5fa' : '#34d399';
    $('#sessV').textContent = fmt4(((r.session)||{}).v||0) + ' · ' + (d.rounds||0) + '轮';
    $('#todayV').textContent = fmt4((r.today||{}).v||0);
    const costBits=[]; if(t.matched&&t.cny)costBits.push('¥'+t.cny); if(t.matched&&t.pts)costBits.push(t.pts+'积分');
    $('#costV').textContent = costBits.length?costBits.join(' + '):'—';
    $('#ioV').textContent = fmt4((r.today||{}).i||0)+' / '+fmt4((r.today||{}).o||0);
    $('#modelV').textContent = esc(d.model||'—');
    let balHtml='';
    try{
      const b = await fetch(API+'/balance',{cache:'no-store'}).then(r=>r.json());
      (b.sources||[]).slice(0,3).forEach(x=>{
        const unit = x.currency==='积分'?'积分':(b.unit||'元');
        balHtml += '<div class="row bal"><span class="bname" title="'+esc(x.name)+'">'+esc(x.name)+'</span><span class="v '+(x.ok?'cost':'')+'">'+(x.ok?(x.balance+' '+esc(unit)):'失败')+'</span></div>';
      });
      if(!(b.sources||[]).length) balHtml = '<div class="row bal"><span class="bname">未配置余额源</span></div>';
    }catch(e){ balHtml = '<div class="row bal"><span class="bname">余额加载失败</span></div>'; }
    $('#balBox').innerHTML = balHtml;
    $('#upd').textContent = new Date().toTimeString().slice(0,8);
  }catch(e){ $('#upd').textContent = '连接失败'; }
}
/* 拖动（仅 iframe 内嵌模式；独立窗口由浏览器标题栏拖动） */
if(!isPopup){
  (function(){
    const w = $('#w'), hd = $('#hd');
    let sx,sy,ox,oy,drag=false;
    let pos=null; try{ pos=JSON.parse(localStorage.getItem('tsWidgetPos')||'null'); }catch(e){}
    const cx = (pos && typeof pos.x==='number') ? pos.x : 16;
    const cy = (pos && typeof pos.y==='number') ? pos.y : 16;
    w.style.left = cx+'px'; w.style.top = cy+'px';
    hd.addEventListener('mousedown',e=>{
      if(e.target.tagName==='BUTTON') return;
      drag=true; w.classList.add('dragging');
      sx=e.clientX; sy=e.clientY;
      const r=w.getBoundingClientRect(); ox=r.left; oy=r.top;
    });
    document.addEventListener('mousemove',e=>{
      if(!drag) return;
      const nx=Math.max(0,Math.min(window.innerWidth-40, ox+e.clientX-sx));
      const ny=Math.max(0,Math.min(window.innerHeight-40, oy+e.clientY-sy));
      w.style.left=nx+'px'; w.style.top=ny+'px';
    });
    document.addEventListener('mouseup',()=>{
      if(!drag) return;
      drag=false; w.classList.remove('dragging');
      try{ localStorage.setItem('tsWidgetPos', JSON.stringify({x:parseInt(w.style.left,10),y:parseInt(w.style.top,10)})); }catch(e){}
    });
  })();
}
tick();
setInterval(tick, 10000);
</script>
</body>
</html>
"""

