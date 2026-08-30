"""KiraAI Token Stats — Token 用量统计看板插件

为 KiraAI 提供完整的 Token 用量统计能力（移植自 Alife 的 1chuxin.TokenStats，
并整合 KiraAI-plugin-api-balance 的查询模式）：

- 逐轮采集：@on.llm_response 钩子记录每轮 LLM 调用的 输入/输出/缓存 tokens，
  包含工具中间步；日志 JSONL 持久化到插件数据目录，重启不丢
- 费用估算：价格规则按 URL > 模型 > 渠道名 加权匹配（4/2/1 分），
  峰谷价（工作日 9:00-12:00 / 14:00-18:00 为峰，其余谷）；
  费用一律在展示时计算，改价后全历史即时重定价
- 余额监测：auto（按 URL 自动分流官方端点 / One-API 中转站）、
  custom（自定义接口多端点尝试 + json_path 取数）、preset（初始额度 − 已计费用）
- 来源归类：自定义关键词规则优先 → 群聊/私聊自动判定 → 工具续轮继承上一轮
- 多入口查询：WebUI 侧边栏仪表盘 / bot 工具（自然语言）/ 可选自定义命令
- 错误统计：「出错：」正则扫描，按范围聚合

模型无关：统计基于 LLMResponse 的 input_tokens/output_tokens/cached_tokens 字段，
任何 Provider 只要上报 tokens 即可统计。
"""

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Request

from core.plugin import BasePlugin, logger, on, Priority, register
from core.plugin.plugin_registry import PluginPage, PageMenu
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.chat import MessageChain
from core.chat.message_elements import Text
from core.provider import LLMResponse

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

# 内置默认价格规则（DeepSeek 官方价，2026-08 抓取；可在配置页修改）
DEFAULT_RULES = [
    {
        "name": "DeepSeek V4-Flash（官方价）",
        "model_match": "flash",
        "peak_enabled": True,
        "hit_peak": 0.10, "hit_off": 0.05,
        "miss_peak": 3.0, "miss_off": 1.5,
        "out_peak": 9.0, "out_off": 4.5,
    },
    {
        "name": "DeepSeek V4-Pro（官方价）",
        "model_match": "pro",
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
    return f"{v:N0}" if v else "0"


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


def _match_rule(rules: list, channel: str, model: str, url: str):
    """URL > 模型 > 渠道名 加权匹配：4/2/1 分，取最高分"""
    best, best_score = None, 0
    for r in rules or []:
        score = 0
        if r.get("url_match") and _url_loose_match(url or "", r.get("url_match") or ""):
            score += 4
        if r.get("model_match") and model and r["model_match"].lower() in str(model).lower():
            score += 2
        if r.get("channel_match") and channel and r["channel_match"].lower() in str(channel).lower():
            score += 1
        if score > best_score:
            best_score, best = score, r
    return best


def _rule_cost(r: dict, input_t: int, output_t: int, cached_t: int, t: datetime) -> float:
    peak = bool(r.get("peak_enabled", True)) and _is_peak(t)
    hit = r.get("hit_peak" if peak else "hit_off", 0) or 0
    miss = r.get("miss_peak" if peak else "miss_off", 0) or 0
    out = r.get("out_peak" if peak else "out_off", 0) or 0
    return (cached_t * hit + max(0, input_t - cached_t) * miss + output_t * out) / 1_000_000


def _read_jsonl(path: Path):
    recs = []
    if not path.exists():
        return recs
    try:
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
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[token_stats] 日志写入失败: {e}")
        return
    # 裁剪：超过 max_size 条时保留最新（0 = 不裁剪）
    if max_size and max_size > 0:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > max_size:
                path.write_text("\n".join(lines[-max_size:]) + "\n", encoding="utf-8")
        except Exception:
            pass


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
        self.source_default = src.get("source_default", "system") or "system"
        self.source_group = src.get("source_group", "qchat") or "qchat"
        self.source_dm = src.get("source_dm", "dm") or "dm"

        # ── 自定义命令 ──
        cmd = cfg.get("section_command", {})
        self.enable_command = bool(cmd.get("enable_command", False))
        self.command_words = cmd.get("command_words", ["/用量", "/token"]) or ["/用量"]
        self.allowed_users = [str(u).strip() for u in (cmd.get("allowed_users", []) or []) if str(u).strip()]
        self.exact_match = bool(cmd.get("exact_match", False))
        self.denied_message = cmd.get("denied_message", "权限不足：您没有查询用量统计的权限")
        self.cmd_success_template = cmd.get("command_success_template", "📊 {provider}：{result}")
        self.cmd_all_template = cmd.get("command_all_template", "📊 Token 用量统计：\n{results}")

        # ── Bot 工具 ──
        tool = cfg.get("section_tool", {})
        self.enable_tool = bool(tool.get("enable_tool", True))
        self.tool_include_balance = bool(tool.get("tool_include_balance", True))

        # ── 价格规则 ──
        pr = cfg.get("section_pricing", {})
        rules = pr.get("rules", None)
        self.rules = rules if isinstance(rules, list) and rules else DEFAULT_RULES

        # ── 余额监测 ──
        bal = cfg.get("section_balance", {})
        self.enable_balance = bool(bal.get("enable_balance", False))
        interval = bal.get("balance_interval", 60)
        self.balance_interval = max(5, int(interval) if interval is not None else 60)
        sources = bal.get("balance_sources", [])
        self.balance_sources = sources if isinstance(sources, list) else []

        # ── 高级 ──
        adv = cfg.get("section_advanced", {})
        max_log = adv.get("max_log_size", 100000)
        self.max_log_size = int(max_log) if max_log is not None else 100000
        idle = adv.get("session_idle_minutes", 30)
        self.session_idle_minutes = max(1, int(idle) if idle is not None else 30)

        # ── 运行时状态 ──
        self._data_dir: Path = None  # initialize 时赋值
        self._log_path: Path = None
        self._lock = asyncio.Lock()

        # 按天聚合：{day: {r,v,i,o,c,e, aggs:{model\u001Fchannel\u001Fhost:[off,peak]}}}
        self._days = {}
        # 单天小时桶：{day: [None]*24 each {r,v,i,o,c,e}}
        self._hours = {}
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

    # ── 生命周期 ──

    async def initialize(self):
        self._data_dir = self.ctx.get_plugin_data_dir()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._data_dir / "usage-log.jsonl"
        self._bal_state_path = self._data_dir / "balance_state.json"

        self._load_history()
        self._load_bal_states()

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

    # ── 历史加载 ──

    def _load_history(self):
        self._days.clear()
        self._hours.clear()
        for rec in _read_jsonl(self._log_path):
            self._apply_rec(rec)
        logger.info(f"[token_stats] 已加载历史 {len(self._days)} 天 / {sum(d['v'] for d in self._days.values())} tokens")

    def _apply_rec(self, rec: dict):
        try:
            t = datetime.fromisoformat(rec.get("t", ""))
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
            hr = hs[t.hour] = {"r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0}
        hr["r"] += 1; hr["v"] += v; hr["i"] += i; hr["o"] += o; hr["c"] += c; hr["e"] += e

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
        peak = _is_peak(datetime.fromisoformat(rec["t"]))
        agg = slots[1 if peak else 0]
        if agg is None:
            agg = slots[1 if peak else 0] = {"i": 0, "o": 0, "c": 0}
        agg["i"] += rec["i"]; agg["o"] += rec["o"]; agg["c"] += rec["c"]

    # ── 余额状态 ──

    def _load_bal_states(self):
        try:
            if self._bal_state_path.exists():
                self._bal_states = json.loads(self._bal_state_path.read_text(encoding="utf-8"))
        except Exception:
            self._bal_states = {}

    def _save_bal_states(self):
        try:
            self._bal_state_path.write_text(
                json.dumps(self._bal_states, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ── 来源 / 渠道识别 ──

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
        """尽力从默认 LLM 客户端取 provider/model/host（防御式，失败回退默认值）"""
        channel, model, host = "默认渠道", "未知", ""
        try:
            client = self.ctx.get_default_llm_client()
            model = (getattr(client, "model_id", None)
                     or getattr(client, "model", None)
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
            channel = host or getattr(client, "provider_id", None) or "默认渠道"
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

    # ── 事件钩子 ──

    @on.im_message(priority=Priority.HIGH)
    async def on_im_message(self, event: KiraMessageEvent, *_):
        """捕获用户文本（来源归类用）+ 自定义命令处理"""
        sid = self._sid(event)
        text = "".join(e.text for e in event.message.chain if isinstance(e, Text))
        if text:
            self._pending[sid] = {"text": text, "source": None, "steps": 0, "at": time.time()}

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

        # 错误统计
        text = (resp.text_response or "") or ""
        pending = self._pending.get(sid)
        if pending is None:
            pending = self._pending[sid] = {"text": "", "source": None, "steps": 0, "at": time.time()}
        errs = len(ERROR_TAG_RE.findall(text)) if text else 0
        if errs > 0:
            self._last_err_text = self._err_snippet(text)
            self._last_err_at = datetime.now()

        # 来源：第一轮（新用户消息）自动判定，工具续轮继承
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
            "t": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
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

        if self.debug_log:
            logger.info(f"[token_stats] rec: +{inp}in/{out}out/{cached}cache "
                        f"src={src} ch={channel} model={model}")

    @staticmethod
    def _err_snippet(text: str) -> str:
        idx = text.find("出错")
        if idx < 0:
            return ""
        start = max(0, idx - 20)
        s = text[start:idx + 40].replace("\r", " ").replace("\n", " ").strip()
        return s[:60]

    # ── 聚合查询 ──

    def _range_agg(self, from_date: str, to_date: str):
        """按天键区间聚合 {v,i,o,c,r,e}（ISO 日期字符串可按序比较）"""
        v = i = o = c = r = e = 0
        for key, ds in self._days.items():
            if key < from_date:
                continue
            if key > to_date:
                break
            v += ds["v"]; i += ds["i"]; o += ds["o"]; c += ds["c"]
            r += ds["r"]; e += ds["e"]
        return {"v": v, "i": i, "o": o, "c": c, "r": r, "e": e}

    def _range_cost(self, from_date: str, to_date: str):
        total, matched = 0.0, False
        for key, ds in self._days.items():
            if key < from_date:
                continue
            if key > to_date:
                break
            for mkey, slots in ds["aggs"].items():
                parts = mkey.split("\u001F")
                model = parts[0] if len(parts) > 0 else ""
                channel = parts[1] if len(parts) > 1 else ""
                host = parts[2] if len(parts) > 2 else ""
                rule = _match_rule(self.rules, channel, model, host)
                if rule is None:
                    continue
                matched = True
                for is_peak, agg in ((False, slots[0]), (True, slots[1])):
                    if agg is None:
                        continue
                    pk = bool(rule.get("peak_enabled", True)) and is_peak
                    hit = rule.get("hit_peak" if pk else "hit_off", 0) or 0
                    miss = rule.get("miss_peak" if pk else "miss_off", 0) or 0
                    out = rule.get("out_peak" if pk else "out_off", 0) or 0
                    total += (agg["c"] * hit + max(0, agg["i"] - agg["c"]) * miss + agg["o"] * out) / 1_000_000
        return total if matched else None

    def _session_cost(self):
        total, matched = 0.0, False
        for mkey, slots in self._sess["aggs"].items():
            parts = mkey.split("\u001F")
            model = parts[0] if len(parts) > 0 else ""
            channel = parts[1] if len(parts) > 1 else ""
            host = parts[2] if len(parts) > 2 else ""
            rule = _match_rule(self.rules, channel, model, host)
            if rule is None:
                continue
            matched = True
            for is_peak, agg in ((False, slots[0]), (True, slots[1])):
                if agg is None:
                    continue
                pk = bool(rule.get("peak_enabled", True)) and is_peak
                hit = rule.get("hit_peak" if pk else "hit_off", 0) or 0
                miss = rule.get("miss_peak" if pk else "miss_off", 0) or 0
                out = rule.get("out_peak" if pk else "out_off", 0) or 0
                total += (agg["c"] * hit + max(0, agg["i"] - agg["c"]) * miss + agg["o"] * out) / 1_000_000
        return total if matched else None

    def _channel_cost(self, url: str, name: str) -> float:
        """某渠道（URL/渠道名包含匹配）在全部历史里的计费总额（preset 扣减用）"""
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
            return 0.0
        total, matched = 0.0, False
        for mkey, slots in merged.items():
            parts = mkey.split("\u001F")
            model = parts[0] if len(parts) > 0 else ""
            channel = parts[1] if len(parts) > 1 else ""
            host = parts[2] if len(parts) > 2 else ""
            rule = _match_rule(self.rules, channel, model, host)
            if rule is None:
                continue
            matched = True
            for is_peak, agg in ((False, slots[0]), (True, slots[1])):
                if agg is None:
                    continue
                pk = bool(rule.get("peak_enabled", True)) and is_peak
                hit = rule.get("hit_peak" if pk else "hit_off", 0) or 0
                miss = rule.get("miss_peak" if pk else "miss_off", 0) or 0
                out = rule.get("out_peak" if pk else "out_off", 0) or 0
                total += (agg["c"] * hit + max(0, agg["i"] - agg["c"]) * miss + agg["o"] * out) / 1_000_000
        return total if matched else 0.0

    # ── 余额探测 ──

    def _bal_state_of(self, name: str) -> dict:
        s = self._bal_states.get(name) or {"balance": 0, "currency": "CNY", "at": "", "ok": False, "msg": "尚未探测"}
        return s

    def _resolve_balance_state(self, src: dict) -> dict:
        """当前额度：填了 initial → 初始 − 已计费用；preset 未填引导；其余取最近探测结果"""
        if src.get("initial"):
            try:
                cost = self._channel_cost(src.get("url", ""), src.get("name", ""))
                cur = float(src["initial"]) - cost
                return {"balance": cur, "currency": src.get("currency", "CNY"),
                        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "ok": True,
                        "msg": f"初始额度 {src['initial']} − 已计费用 {cost:.4f} = 当前 {cur:.4f}（按价格规则估算）"}
            except Exception as e:
                return {"balance": 0, "currency": src.get("currency", "CNY"),
                        "at": "", "ok": False, "msg": f"初始额度扣减失败: {e}"}
        if (src.get("type") or "auto").strip().lower() == "preset":
            return {"balance": 0, "currency": src.get("currency", "CNY"),
                    "at": "", "ok": False,
                    "msg": "preset 源需先在配置页填「初始额度」（当前额度 = 初始额度 − 已计费用）"}
        return self._bal_state_of(src.get("name", ""))

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

    async def _probe_one(self, src: dict) -> dict:
        """探测单个余额源（auto/custom），失败返回 Ok=false + 原因"""
        st = {"balance": 0, "currency": src.get("currency", "CNY"),
              "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": False, "msg": ""}
        try:
            s_type = (src.get("type") or "auto").strip().lower()
            api_key = src.get("api_key", "") or ""
            jpath = (src.get("json_path") or "").strip()

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

    async def _http_get(self, url: str, api_key: str):
        """GET 请求，返回解析后的 JSON；非 2xx 抛异常"""
        if aiohttp is None:
            raise RuntimeError("aiohttp 未安装")
        headers = {}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        timeout = aiohttp.ClientTimeout(total=10)
        try:
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

    async def _probe_all(self):
        if self._bal_busy:
            return
        self._bal_busy = True
        try:
            for src in self.balance_sources:
                if not src.get("enabled", True):
                    continue
                name = src.get("name", "")
                if not name:
                    continue
                if src.get("initial") or (src.get("type") or "").strip().lower() == "preset":
                    st = self._resolve_balance_state(src)
                else:
                    st = await self._probe_one(src)
                self._bal_states[name] = st
            self._save_bal_states()
        except Exception as e:
            logger.warning(f"[token_stats] 余额探测异常: {e}")
        finally:
            self._bal_busy = False

    async def _balance_loop(self):
        try:
            while True:
                await self._probe_all()
                await asyncio.sleep(max(300, self.balance_interval * 60))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[token_stats] 余额轮询循环退出")

    # ── 查询回复构建（命令 / 工具共用）──

    def _build_summary_text(self, range_key: str = "") -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        sb = []
        channel, model, _ = self._resolve_channel_model()
        sb.append(f"【Token 用量统计】渠道 {channel} · 模型 {model}")

        def want(k):
            return not range_key or range_key.strip().lower() == k

        if want("session"):
            s = self._sess
            cost = self._session_cost()
            line = f"本次会话：{_fmt_num(s['v'])} tokens · 输入 {_fmt_num(s['i'])} · 输出 {_fmt_num(s['o'])} · 缓存 {_fmt_num(s['c'])} · {s['r']} 轮"
            if cost is not None:
                line += f" · 费用 ¥{cost:.4f}"
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
            cost = self._range_cost(frm, to)
            line = f"{label}：{_fmt_num(agg['v'])} tokens · 输入 {_fmt_num(agg['i'])} · 输出 {_fmt_num(agg['o'])} · 缓存 {_fmt_num(agg['c'])} · {agg['r']} 轮"
            if cost is not None:
                line += f" · 费用 ¥{cost:.4f}"
            if agg["e"] > 0:
                line += f" · 出错 {agg['e']}"
            sb.append(line)

        if self.tool_include_balance and self.enable_balance and self.balance_sources:
            sb.append("账户余额：")
            for src in self.balance_sources:
                if not src.get("enabled", True):
                    continue
                st = self._resolve_balance_state(src)
                name = src.get("name", "")
                if src.get("initial"):
                    sb.append(f"- {name}：{st['balance']:.4f} {st['currency']}（初始额度 − 已用）")
                elif st["ok"]:
                    sb.append(f"- {name}：{st['balance']:.4f} {st['currency']}（{st.get('at', '')[:16]} 更新）")
                else:
                    sb.append(f"- {name}：探测失败（{st['msg']}）")

        if self._last_err_text:
            sb.append(f"最近出错：{self._last_err_text}")
        return "\n".join(sb)

    async def _build_query_reply(self, arg: str) -> str:
        arg = arg.strip().lower()
        aliases = {"本次": "session", "今天": "today", "7天": "d7", "30天": "d30", "累计": "total",
                   "余额": "balance"}
        key = aliases.get(arg, arg)
        if key in RANGES:
            text = self._build_summary_text(key)
            return self.cmd_success_template.format(provider=RANGE_LABELS[key], result=text)
        if key == "balance":
            if not self.enable_balance or not self.balance_sources:
                return "未启用余额监测或未配置余额源（插件配置页 → 余额监测）"
            await self._probe_all()
            lines = ["💳 账户余额："]
            for src in self.balance_sources:
                if not src.get("enabled", True):
                    continue
                st = self._resolve_balance_state(src)
                name = src.get("name", "")
                if src.get("initial"):
                    lines.append(f"- {name}：{st['balance']:.4f} {st['currency']}（初始额度 − 已用）")
                elif st["ok"]:
                    lines.append(f"- {name}：{st['balance']:.4f} {st['currency']}（{st.get('at', '')[:16]} 更新）")
                else:
                    lines.append(f"- {name}：探测失败（{st['msg']}）")
            return "\n".join(lines)
        # 默认：全部概览
        return self._build_summary_text("")

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
            return self._build_summary_text(range or "")
        except Exception as e:
            logger.exception("[token_stats] tool query failed")
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
        costs = {
            "session": self._session_cost(),
            "today": self._range_cost(today, today),
            "d7": self._range_cost(d7, today),
            "d30": self._range_cost(d30, today),
            "total": self._range_cost("0000-01-01", "9999-12-31"),
        }
        errors = {
            "session": s.get("e", 0),
            "today": self._range_agg(today, today)["e"],
            "total": self._range_agg("0000-01-01", "9999-12-31")["e"],
            "last": self._last_err_text,
        }
        # 余额摘要：当前渠道匹配的源
        bal_summary = {"sources": len(self.balance_sources), "ok": 0, "current": ""}
        for src in self.balance_sources:
            if not src.get("enabled", True):
                continue
            st = self._resolve_balance_state(src)
            if st["ok"]:
                bal_summary["ok"] += 1
                if not bal_summary["current"]:
                    bal_summary["current"] = f"{src.get('name', '')} {st['balance']:.2f} {st['currency']}"

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
        recs = _read_jsonl(self._log_path)
        recs.reverse()
        out = []
        for r in recs[:n]:
            try:
                t = datetime.fromisoformat(r["t"])
            except Exception:
                continue
            cost = None
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            if rule:
                cost = _rule_cost(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t)
            out.append({
                "t": t.strftime("%Y-%m-%d %H:%M:%S"),
                "v": r.get("v", 0), "i": r.get("i", 0), "o": r.get("o", 0), "c": r.get("c", 0),
                "m": r.get("m", ""), "s": r.get("s", ""), "ch": r.get("ch", ""),
                "h": r.get("h", ""), "sid": r.get("sid", ""),
                "co": f"{cost:.4f}" if cost is not None else None,
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
        total = {"r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "cost": 0.0, "matched": False}
        for r in _read_jsonl(self._log_path):
            try:
                t = datetime.fromisoformat(r["t"])
            except Exception:
                continue
            day = t.strftime("%Y-%m-%d")
            if day < frm or day > to:
                continue
            i, o, c, v = r.get("i", 0), r.get("o", 0), r.get("c", 0), r.get("v", 0)
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            cost = _rule_cost(rule, i, o, c, t) if rule else None

            def add(d, k):
                a = d.setdefault(k, {"r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "cost": 0.0, "matched": False})
                a["r"] += 1; a["i"] += i; a["o"] += o; a["c"] += c; a["v"] += v
                if cost is not None:
                    a["cost"] += cost; a["matched"] = True

            add(by_source, r.get("s", "") or "未知")
            add(by_channel, r.get("ch", "") or "未知")
            add(by_model, r.get("m", "") or "未知")
            add(by_sid, r.get("sid", "") or "未知")
            total["r"] += 1; total["i"] += i; total["o"] += o; total["c"] += c; total["v"] += v
            if cost is not None:
                total["cost"] += cost; total["matched"] = True

        def dim(d):
            arr = [{"k": k, **v} for k, v in d.items()]
            arr.sort(key=lambda x: x["v"], reverse=True)
            return arr[:20]

        return {
            "from": frm, "to": to,
            "total": {"r": total["r"], "i": total["i"], "o": total["o"], "c": total["c"], "v": total["v"],
                      "cost": f"{total['cost']:.4f}" if total["matched"] else None},
            "bySource": dim(by_source),
            "byChannel": dim(by_channel),
            "byModel": dim(by_model),
            "bySid": dim(by_sid),
        }

    @register.api(method="GET", path="/balance", auth=True)
    async def api_balance(self, request: Request):
        """/balance?refresh=1 → 先强制即时探测再返回"""
        if (request.query_params.get("refresh") or "") == "1":
            await self._probe_all()
        sources = []
        for src in self.balance_sources:
            if not src.get("enabled", True):
                continue
            st = self._resolve_balance_state(src)
            sources.append({
                "name": src.get("name", ""),
                "type": (src.get("type") or "auto").strip().lower(),
                "initial": bool(src.get("initial")),
                "ok": st["ok"],
                "balance": f"{st['balance']:.4f}" if st["ok"] else "",
                "currency": st.get("currency", "CNY"),
                "at": st.get("at", ""),
                "msg": st.get("msg", ""),
            })
        return {"interval": max(5, self.balance_interval), "sources": sources}

    @register.api(method="GET", path="/pricing", auth=True)
    async def api_pricing(self):
        return {"rules": self.rules}

    # ── WebUI 侧边栏页面 ──

    @register.page("/stats", auth=True, menu=PageMenu(label={"zh": "Token 用量"}, icon="DataLine"))
    def page_stats(self):
        return PluginPage.from_html(_DASHBOARD_HTML)


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Token 用量统计</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--fg:#e2e8f0;--dim:#94a3b8;--acc:#38bdf8;--ok:#34d399;--warn:#fbbf24;--err:#f87171;--purple:#a78bfa}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:"Segoe UI",system-ui,"Microsoft YaHei",sans-serif;padding:20px;font-size:14px}
h1{font-size:20px;margin-bottom:4px;display:flex;align-items:center;gap:10px}
h1 .dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.tabs{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
.tab{padding:6px 16px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--dim);cursor:pointer;font-size:13px}
.tab.on{background:var(--acc);color:#06283d;font-weight:600;border-color:var(--acc)}
.panel{display:none}.panel.on{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .k{color:var(--dim);font-size:11px;letter-spacing:.5px}
.card .v{font-size:22px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}
.card .v.cost{color:var(--ok)}.card .v.in{color:var(--acc)}.card .v.out{color:#f472b6}.card .v.cache{color:var(--warn)}
.card .d{color:var(--dim);font-size:11px;margin-top:3px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.box h3{font-size:14px;margin-bottom:10px;color:var(--fg)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--dim);text-align:left;font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line);font-size:11px;letter-spacing:.5px}
td{padding:6px 8px;border-bottom:1px solid rgba(51,65,85,.4);font-variant-numeric:tabular-nums}
tr:hover td{background:rgba(56,189,248,.05)}
.rate{color:var(--purple)}.ok{color:var(--ok)}.bad{color:var(--err)}
.bar{height:8px;border-radius:4px;background:#0b1220;overflow:hidden;margin-top:4px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--purple));border-radius:4px}
.hours{display:grid;grid-template-columns:repeat(12,1fr);gap:6px}
.hours .h{background:#0b1220;border-radius:6px;padding:6px;text-align:center;font-size:10px;color:var(--dim)}
.hours .h i{display:block;height:46px;background:#0b1220;border-radius:3px;margin:4px 0 2px;position:relative;overflow:hidden}
.hours .h i b{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(180deg,var(--acc),#6366f1);border-radius:3px 3px 0 0}
.btn{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:5px 14px;cursor:pointer;font-size:12px}
.btn:hover{border-color:var(--acc)}
.btn:disabled{opacity:.5;cursor:default}
.snapshot{display:flex;gap:16px;align-items:center;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin-bottom:16px;font-size:12.5px}
.snapshot .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block;margin-right:6px;box-shadow:0 0 6px var(--ok)}
.snapshot .st{color:var(--dim)}
.snapshot b{color:var(--fg)}
.errbox{border-left:3px solid var(--err)}
.note{color:var(--dim);font-size:11.5px;margin-top:8px;line-height:1.6}
</style>
</head>
<body>
<h1><span class="dot" id="dot"></span>Token 用量统计</h1>
<div class="sub" id="sub">加载中…</div>
<div class="tabs">
  <div class="tab on" data-p="ov">概览</div>
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
    <div class="box" id="hourBox"><h3>今日按小时</h3><div id="hours"></div></div>
  </div>
</div>

<div class="panel" id="p-dim">
  <div class="box" style="margin-bottom:12px">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
      <button class="btn" data-r="today">今天</button>
      <button class="btn" data-r="d7">近7天</button>
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
      <span style="color:var(--dim);font-size:12px">最近 15 轮（含工具步），费用按当前价格规则即时计算</span>
    </div>
    <table><thead><tr><th>时间</th><th>模型</th><th>来源</th><th>渠道</th><th>输入</th><th>输出</th><th>缓存</th><th>总量</th><th>费用</th></tr></thead><tbody id="recBody"></tbody></table>
  </div>
</div>

<div class="panel" id="p-price">
  <div class="box" id="priceBox"><h3>价格规则</h3><div id="priceBody"></div></div>
</div>

<div class="panel" id="p-bal">
  <div class="box">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
      <button class="btn" id="balRefresh">立即探测</button>
      <span style="color:var(--dim);font-size:12px" id="balInfo"></span>
    </div>
    <table><thead><tr><th>名称</th><th>类型</th><th>余额</th><th>更新时间</th><th>状态</th></tr></thead><tbody id="balBody"></tbody></table>
    <div class="note">配置入口：插件管理 → KiraAI_token_stats_plugin → 配置 → 「余额监测」。类型说明：auto=按 URL 自动探测官方端点或 One-API 中转站；custom=自定义接口多端点尝试；preset=初始额度 − 已计费用。</div>
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

document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('on'));
  t.classList.add('on'); $('#p-'+t.dataset.p).classList.add('on');
  if(t.dataset.p==='dim') loadDim('today');
  if(t.dataset.p==='rec') loadRec();
  if(t.dataset.p==='price') loadPrice();
  if(t.dataset.p==='bal') loadBal(false);
});

async function jget(p){ const r = await fetch(API+p, {cache:'no-store'}); return r.json(); }

const RL = {session:'本次会话',today:'今天',d7:'近7天',d30:'近30天',total:'累计'};
async function loadOv(){
  const d = await jget('/stats');
  $('#dot').style.background = d.busy ? '#60a5fa' : '#34d399';
  $('#sub').textContent = '模型 ' + (d.model||'—') + ' · 渠道 ' + (d.channel||'—') + ' · 日志 ' + (d.logFile||'');
  const el = Math.floor(d.elapsed/60), em = d.elapsed%60;
  $('#snap').innerHTML = '<span><span class="dot"></span>' + esc(d.src||'—') + '</span>' +
    '<span class="st">会话 <b>'+d.rounds+'</b> 轮 · <b>'+fmt4(d.total)+'</b> Token · 已进行 '+(el>0?el+' 分 ':'')+em+' 秒</span>' +
    '<span class="st">最近一轮：输入 <b>'+fmt(d.lastInput)+'</b> · 输出 <b>'+fmt(d.lastOutput)+'</b>'+(d.lastCached?' · 缓存 <b>'+fmt(d.lastCached)+'</b>':'')+'</span>';
  const cards = [];
  for (const k of ['session','today','d7','d30','total']){
    const rg = (d.ranges||{})[k]||{}, co = (d.costs||{})[k];
    const rate = rg.i>0 ? (rg.c/rg.i*100).toFixed(1)+'%' : '—';
    const errs = (d.errors||{})[k]||0;
    cards.push('<div class="card"><div class="k">'+RL[k]+'</div><div class="v">'+fmt4(rg.v)+'</div>'+
      '<div class="d">'+fmt(rg.r)+' 轮 · 输入 '+fmt(rg.i)+' · 输出 '+fmt(rg.o)+' · 缓存 '+fmt(rg.c)+' · 命中率 <span class="rate">'+rate+'</span></div>'+
      '<div class="d">费用 <span class="cost">'+(co!=null?'¥'+co:'—')+'</span>'+(errs?' · 出错 <span class="bad">'+errs+'</span>':'')+'</div></div>');
  }
  const er = d.errors||{};
  if (er.last) cards.push('<div class="card errbox"><div class="k">最近出错</div><div class="d">'+esc(er.last)+'</div></div>');
  $('#cards').innerHTML = cards.join('');
  loadHist(); loadHours();
}
async function loadHist(){
  const d = await jget('/history');
  const days = (d.days||[]).slice(-14);
  if(!days.length){ $('#hist').innerHTML='<div class="note">暂无历史数据</div>'; return; }
  const max = Math.max(...days.map(x=>x.v),1);
  $('#hist').innerHTML = '<table><thead><tr><th>日期</th><th>总量</th><th>轮数</th><th>输入</th><th>输出</th><th>缓存</th><th style="width:30%">分布</th></tr></thead><tbody>'+
    days.map(x=>'<tr><td>'+x.d+'</td><td>'+fmt4(x.v)+'</td><td>'+x.r+'</td><td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td>'+
    '<td><div class="bar"><i style="width:'+(x.v/max*100)+'%"></i></div></td></tr>').join('')+'</tbody></table>';
}
async function loadHours(){
  const d = await jget('/history?day='+localDate());
  const hs = d.hours||[];
  const max = Math.max(...hs.map(x=>x.v),1);
  const cells = [];
  for(let h=0;h<24;h++){
    const x = hs.find(y=>y.h===h);
    cells.push('<div class="h">'+h+'时<i>'+(x?'<b style="height:'+Math.max(4,x.v/max*100)+'%"></b>':'')+'</i>'+(x?fmt4(x.v):'—')+'</div>');
  }
  $('#hours').innerHTML = '<div class="hours">'+cells.join('')+'</div>';
}

let dimRange='today';
document.querySelectorAll('[data-r]').forEach(b=>b.onclick=()=>{dimRange=b.dataset.r;loadDim(dimRange)});
async function loadDim(r){
  const d = await jget('/analytics?range='+r);
  $('#dimRange').textContent = d.from + ' ~ ' + d.to + '（' + RL[r] + '）';
  const rows = [];
  const push = (name, arr) => (arr||[]).forEach(x=>{
    rows.push('<tr><td>'+esc(name)+'</td><td>'+esc(x.k)+'</td><td>'+x.r+'</td><td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td><td>'+fmt(x.v)+'</td><td>'+(x.cost!=null?'¥'+x.cost:'—')+'</td></tr>');
  });
  push('来源', d.bySource); push('渠道', d.byChannel); push('模型', d.byModel); push('会话', d.bySid);
  $('#dimBody').innerHTML = rows.join('') || '<tr><td colspan="8" class="note">该范围暂无数据</td></tr>';
}
async function loadRec(){
  const d = await jget('/records?n=15');
  $('#recBody').innerHTML = (d.recs||[]).map(x=>
    '<tr><td>'+esc(x.t)+'</td><td>'+esc(x.m)+'</td><td>'+esc(x.s)+'</td><td>'+esc(x.ch)+'</td>'+
    '<td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td><td>'+fmt(x.v)+'</td><td>'+(x.co?'¥'+x.co:'—')+'</td></tr>').join('') ||
    '<tr><td colspan="9" class="note">暂无记录</td></tr>';
}
async function loadPrice(){
  const d = await jget('/pricing');
  const rules = d.rules||[];
  $('#priceBody').innerHTML = rules.length ? '<table><thead><tr><th>名称</th><th>URL 匹配</th><th>模型匹配</th><th>渠道匹配</th><th>峰谷</th><th>缓存命中(峰/谷)</th><th>未命中(峰/谷)</th><th>输出(峰/谷)</th></tr></thead><tbody>'+
    rules.map(r=>'<tr><td>'+esc(r.name||'')+'</td><td>'+esc(r.url_match||'')+'</td><td>'+esc(r.model_match||'')+'</td><td>'+esc(r.channel_match||'')+'</td>'+
    '<td>'+(r.peak_enabled?'峰谷':'恒谷')+'</td><td>'+r.hit_peak+' / '+r.hit_off+'</td><td>'+r.miss_peak+' / '+r.miss_off+'</td><td>'+r.out_peak+' / '+r.out_off+'</td></tr>').join('')+'</tbody></table>'+
    '<div class="note">编辑入口：插件管理 → 配置 → 「价格规则」。匹配加权 URL=4 分、模型=2 分、渠道名=1 分取最高；价格单位 元/百万 tokens；峰=工作日 9:00-12:00、14:00-18:00。</div>'
    : '<div class="note">暂无价格规则，费用显示「—」</div>';
}
async function loadBal(refresh){
  const d = await jget('/balance'+(refresh?'?refresh=1':''));
  $('#balInfo').textContent = '轮询间隔 ' + d.interval + ' 分钟';
  $('#balBody').innerHTML = (d.sources||[]).map(x=>
    '<tr><td>'+esc(x.name)+'</td><td>'+esc(x.type)+'</td><td class="'+(x.ok?'ok':'bad')+'">'+(x.ok?('¥'+x.balance+' '+esc(x.currency)):'失败')+'</td>'+
    '<td>'+esc(x.at||'—')+'</td><td>'+esc(x.msg||'—')+'</td></tr>').join('') ||
    '<tr><td colspan="5" class="note">未配置余额监测源（插件配置页 → 余额监测）</td></tr>';
}
$('#balRefresh').onclick = ()=>{ $('#balRefresh').disabled=true; loadBal(true).finally(()=>$('#balRefresh').disabled=false); };
$('#recRefresh').onclick = loadRec;

loadOv();
setInterval(loadOv, 5000);
</script>
</body>
</html>
"""
