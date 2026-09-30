"""第五步：Telegram bot（long polling）——把第四步的問答核心接到 Telegram。

結構：
- 純函式：白名單、指令解析、HTML 格式轉換、切訊息、每日花費加總、對話自動重置、醫案原文的格式。
- BotCore：處理一則訊息的流程。只用到 bot 物件的 send_message、edit_message_text、send_chat_action
  三個 async 方法（python-telegram-bot 的 Bot 就有），測試用假的 bot 物件。
- build_application()：接上 python-telegram-bot（只有這裡 import telegram）。

執行緒：Answerer.ask 是同步、會阻塞的，SQLite 連線也不跨執行緒用，所以所有問答與查原文都丟給
單一專用 worker 執行緒（ThreadPoolExecutor(max_workers=1)），Searcher 與 Answerer 也在那個執行緒裡建立。
同一時間只處理一題，其餘依序排隊。
"""

import asyncio
from collections import Counter
import html
import json
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from haixia import answer as core
from haixia.search import citation, display_source_path, original_title

log = logging.getLogger("haixia.bot")

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")   # 台灣沒有日光節約時間，用固定時差
TWD_PER_USD = 32
TELEGRAM_LIMIT = 4096
ANSWER_LIMIT = 3900            # 依解析後純文字計，留給 Telegram 一點餘裕
DEFAULT_DAILY_BUDGET = 3.0
IDLE_RESET_SEC = 6 * 3600
CONTEXT_RESET_TOKENS = 150_000
PROGRESS_INTERVAL = 2.0         # 進度訊息最多每 2 秒改一次
TYPING_INTERVAL = 4.5           # Telegram 的「輸入中」約 5 秒後消失
PROGRESS_QUERIES = 4            # 進度訊息最多列幾個查詢
SERVICE = "haixia-bot"
ALLOWED_UPDATES = ("message", "callback_query")

MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
          "gemini-flash": "gemini-3.8-flash", "gemini-pro": "gemini-3.1-pro-preview"}
MODEL_NAMES = {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5",
               "gemini-3.8-flash": "Gemini 3.8 Flash", "gemini-3.1-pro-preview": "Gemini 3.1 Pro"}
COMMANDS = {"start", "help", "new", "model", "cost", "source"}
BOT_COMMANDS = [("help", "說明"), ("new", "開新對話"), ("model", "換模型"),
                ("cost", "看花費"), ("source", "看原文：/source 編號")]
MODEL_CALLBACKS = {"m:o": "opus", "m:s": "sonnet", "m:f": "gemini-flash", "m:p": "gemini-pro"}

BM25_NOTE = "（語意搜尋失敗，這題只用關鍵字）"


def model_name(model):
    return MODEL_NAMES.get(model, model)


def model_menu(model, provider=None):
    """傳回選單文字與按鈕規格；callback data 固定且短。"""
    current = f"目前模型：{model_name(model)}"
    if provider is None:
        return current + "\n選服務：", [[("Claude", "m:c"), ("Gemini", "m:g")]]
    aliases = ("opus", "sonnet") if provider == "claude" else ("gemini-flash", "gemini-pro")
    rows = [[(model_name(MODELS[alias]) + ("（目前）" if MODELS[alias] == model else ""), data)]
            for alias, data in zip(aliases, ("m:o", "m:s") if provider == "claude" else ("m:f", "m:p"))]
    rows.append([("‹ 返回", "m:b")])
    return current + f"\n選 {'Claude' if provider == 'claude' else 'Gemini'} 模型：", rows


def model_keyboard(rows):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                 for row in rows])


# ---------- 設定 ----------

class ConfigError(RuntimeError):
    pass


def parse_allowed_ids(text):
    """「123, 456」→ frozenset({123, 456})。空的或格式不對丟 ConfigError（使用者 ID 不是機密，可以寫進訊息）。"""
    ids = set()
    for part in re.split(r"[,，\s]+", (text or "").strip()):
        if not part:
            continue
        if not re.fullmatch(r"-?\d+", part):
            raise ConfigError(f"TELEGRAM_ALLOWED_USER_IDS 只能是逗號分隔的數字，這一項不是：{part}")
        ids.add(int(part))
    if not ids:
        raise ConfigError("TELEGRAM_ALLOWED_USER_IDS 是空的：至少要有一個允許的 Telegram 使用者 ID")
    return frozenset(ids)


def parse_budget(text):
    if text is None or not str(text).strip():
        return DEFAULT_DAILY_BUDGET
    try:
        value = float(str(text).strip())
    except ValueError:
        raise ConfigError(f"DAILY_BUDGET_USD 要是數字（美元），現在是：{text}") from None
    if not value > 0:
        raise ConfigError("DAILY_BUDGET_USD 要大於 0")
    return value


@dataclass
class Config:
    token: str
    allowed: frozenset
    daily_budget: float
    api_key: str
    workspace_id: str | None


def load_config(environ=None, env_path=None):
    """讀 bot 的設定；規則和 ANTHROPIC_API_KEY 相同：環境變數優先，沒有才讀 .env。錯誤訊息不含 token。"""
    token = core.env_setting("TELEGRAM_BOT_TOKEN", environ, env_path)
    if not token:
        raise ConfigError("找不到 TELEGRAM_BOT_TOKEN：請設定環境變數，或寫進 repo 根目錄的 .env（TELEGRAM_BOT_TOKEN=…）")
    allowed_text = core.env_setting("TELEGRAM_ALLOWED_USER_IDS", environ, env_path)
    budget_text = core.env_setting("DAILY_BUDGET_USD", environ, env_path)
    try:
        api_key = core.resolve_api_key(environ, env_path)
    except core.MissingApiKey as problem:
        raise ConfigError(str(problem)) from None
    workspace_id = core.resolve_workspace_id(environ, env_path)
    try:
        if not allowed_text:
            raise ConfigError("找不到 TELEGRAM_ALLOWED_USER_IDS：請在 .env 寫允許的 Telegram 使用者 ID（逗號分隔）")
        allowed = parse_allowed_ids(allowed_text)
        budget = parse_budget(budget_text)
    except ConfigError as problem:
        raise ConfigError(core.redact(str(problem), token, api_key, workspace_id)) from None
    return Config(token, allowed, budget, api_key, workspace_id)


class RedactFilter(logging.Filter):
    """把 log 裡的機密（bot token、API 金鑰）換成 ***，包括例外的 traceback。掛在 handler 上。"""

    def __init__(self, *secrets):
        super().__init__()
        self.secrets = [secret for secret in secrets if secret]

    def filter(self, record):
        record.msg = core.redact(record.getMessage(), *self.secrets)
        record.args = ()
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = core.redact(text, *self.secrets)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = core.redact(record.exc_text, *self.secrets)
        return True


# ---------- 指令 ----------

@dataclass(frozen=True)
class Command:
    name: str           # start、help、new、model、cost、source，其他的是 unknown
    arg: str = ""
    raw: str = ""


_COMMAND = re.compile(r"^/([A-Za-z0-9_]+)(?:@\w+)?(?:\s+(.*))?$", re.S)
_ORIGINAL = re.compile(r"^原文\s*[:：]?\s*(?:編號\s*)?([0-9A-Fa-f]{4,16})$")
_CODE = re.compile(r"^(?:編號\s*[:：]?\s*)?([0-9A-Fa-f]{4,16})$")


def parse_command(text):
    """指令 → Command；一般問題回傳 None。「原文 3e13af」等同 /source 3e13af。"""
    text = (text or "").strip()
    found = _COMMAND.match(text)
    if found:
        name = found.group(1).lower()
        if re.fullmatch(r"s_[0-9a-f]{4,16}", name) and not found.group(2):
            return Command("source", name[2:], found.group(1))
        return Command(name if name in COMMANDS else "unknown", (found.group(2) or "").strip(), found.group(1))
    found = _ORIGINAL.match(text)
    if found:
        return Command("source", found.group(1), "原文")
    return None


def resolve_model(arg):
    """opus／sonnet（或完整模型 id）→ 模型 id；不認得回傳 None。"""
    arg = (arg or "").strip().lower()
    if arg in MODELS:
        return MODELS[arg]
    if arg in MODELS.values():
        return arg
    return None


def normalize_code(arg):
    found = _CODE.match((arg or "").strip())
    return found.group(1).lower() if found else None


# ---------- 格式 ----------

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
_BRACKET_HEADING = re.compile(r"^【[^】\n]+】[:：]?$")
_TOKENS = re.compile(r"<[^>]*>|&#?\w+;|.", re.S)


def utf16_len(text):
    """Telegram 以 UTF-16 單位計算長度（擴充區的漢字算 2）。"""
    return len(text.encode("utf-16-le")) // 2


def to_html(text):
    """純文字（Claude 的答案）→ Telegram HTML：先跳脫 < > &，再把 **粗體** 與標題行轉成 <b>。

    每一行各自處理，所以標籤一定在同一行內成對，切訊息時只要不切在行中間就不會切到標籤。
    """
    lines = []
    for line in text.split("\n"):
        escaped = html.escape(line, quote=False)
        stripped = escaped.strip()
        heading = _MD_HEADING.match(stripped)
        plain = stripped.replace("**", "")
        if heading:
            lines.append(f"<b>{heading.group(1).replace('**', '')}</b>")
        elif plain and _BRACKET_HEADING.match(plain):
            lines.append(f"<b>{plain}</b>")
        else:
            lines.append(_BOLD.sub(r"<b>\1</b>", escaped))
    return "\n".join(lines)


def html_to_plain(text):
    return html.unescape(re.sub(r"<[^>]*>", "", text))


def _hard_split(line, limit):
    """單一行超過上限時逐字切開；不切在標籤或 &amp; 中間，切點在粗體裡時先關再開。"""
    parts, current, size, bold = [], "", 0, False
    for token in _TOKENS.findall(line):
        width = utf16_len(token)
        if token == "</b>":
            need = width                     # 關閉標籤的空間已經預留
        elif token == "<b>":
            need = width + 1 + 4             # 至少要放得下一個字和 </b>
        else:
            need = width + (4 if bold else 0)
        if current and size + need > limit:
            if bold:
                current += "</b>"
            parts.append(current)
            current, size = ("<b>", 3) if bold else ("", 0)
        current += token
        size += width
        if token == "<b>":
            bold = True
        elif token == "</b>":
            bold = False
    if current:
        parts.append(current)
    return parts


def split_html(text, limit=TELEGRAM_LIMIT):
    """切成每則不超過 limit 的訊息，優先切在段落邊界，其次換行；不會切在標籤中間。"""
    if utf16_len(text) <= limit:
        return [text] if html_to_plain(text).strip() else []
    chunks, current = [], ""
    for separator, part in _flatten(text, limit):
        candidate = current + separator + part if current else part
        if utf16_len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = part
    chunks.append(current)
    return [chunk.strip("\n") for chunk in chunks if html_to_plain(chunk).strip()]


def _flatten(text, limit):
    """→ [(前面的分隔符號, 小塊)]；段落之間是空行，段落內是換行，硬切的小塊之間不加分隔。"""
    result = []
    for index, paragraph in enumerate(text.split("\n\n")):
        separator = "\n\n" if index else ""
        if utf16_len(paragraph) <= limit:
            result.append((separator, paragraph))
            continue
        for line_index, line in enumerate(paragraph.split("\n")):
            line_separator = separator if line_index == 0 else "\n"
            if utf16_len(line) <= limit:
                result.append((line_separator, line))
                continue
            for part_index, part in enumerate(_hard_split(line, limit)):
                result.append((line_separator if part_index == 0 else "", part))
    return result


# ---------- 花費 ----------

PROVIDERS = ("Claude", "Gemini")        # 分屬 Anthropic 與 GCP 兩份帳單；其他模型只在有紀錄時列出
OTHER = "其他"


def money(amount, twd=True):
    """金額格式：「0.117 USD ≈ 3.8 TWD」。答案標題、/cost、每日上限共用。"""
    text = f"{amount:.3f} USD"
    return text + f" ≈ {amount * TWD_PER_USD:.1f} TWD" if twd else text


def provider_of(model):
    """JSONL 的 model 欄位 → Claude、Gemini 或其他。"""
    model = model if isinstance(model, str) else ""
    if model.startswith("claude-"):
        return "Claude"
    if model.startswith("gemini-"):
        return "Gemini"
    return OTHER


@dataclass
class CostTotal:
    count: int = 0
    usd: float = 0.0


@dataclass
class CostSummary:
    day: str                                    # 台灣時間的日期，例如 2026-09-30
    today: dict = field(default_factory=dict)   # 服務 → CostTotal
    month: dict = field(default_factory=dict)

    @property
    def today_count(self):
        return sum(total.count for total in self.today.values())

    @property
    def today_usd(self):
        return sum(total.usd for total in self.today.values())

    @property
    def month_count(self):
        return sum(total.count for total in self.month.values())

    @property
    def month_usd(self):
        return sum(total.usd for total in self.month.values())


def _add(totals, provider, cost):
    total = totals.setdefault(provider, CostTotal())
    total.count += 1
    total.usd += cost


def summarize_costs(lines, now):
    """JSONL 紀錄（answer.py 每題一行）→ 今天、本月（台灣時間）各服務的題數與花費。壞掉的行略過。"""
    local = now.astimezone(TAIPEI)
    summary = CostSummary(day=local.strftime("%Y-%m-%d"))
    for line in lines:
        try:
            entry = json.loads(line)
            when = datetime.fromisoformat(entry["time"])
            cost = float(entry.get("cost_usd") or 0.0)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if not math.isfinite(cost):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.astimezone(TAIPEI)
        if (when.year, when.month) != (local.year, local.month):
            continue
        provider = provider_of(entry.get("model"))
        _add(summary.month, provider, cost)
        if when.date() == local.date():
            _add(summary.today, provider, cost)
    return summary


def read_costs(log_path, now):
    try:
        with open(log_path, encoding="utf-8") as source:
            return summarize_costs(source, now)
    except FileNotFoundError:
        return summarize_costs([], now)


def budget_message(summary, budget):
    return (f"今天花了 {money(summary.today_usd)}，到上限 {money(budget, twd=False)} 了，明天 0 點重算。\n"
            "要調整上限，改 .env 的 DAILY_BUDGET_USD。")


def _cost_lines(totals):
    names = PROVIDERS + ((OTHER,) if OTHER in totals else ())
    lines = []
    for name in names:
        total = totals.get(name, CostTotal())
        lines.append(f"{name}　{total.count} 題　{money(total.usd)}")
    lines.append(f"合計　　{money(sum(total.usd for total in totals.values()))}")
    return lines


def cost_message(summary, budget):
    day = datetime.strptime(summary.day, "%Y-%m-%d")
    left = max(budget - summary.today_usd, 0.0)
    return "\n".join([f"今天 {day.month}/{day.day}", *_cost_lines(summary.today), "",
                      "本月", *_cost_lines(summary.month), "",
                      f"每日上限 {money(budget, twd=False)}，今天還剩 {money(left, twd=False)}"])


# ---------- 對話 ----------

@dataclass
class ChatState:
    model: str = core.DEFAULT_MODEL
    conversation: core.Conversation = field(default_factory=core.Conversation)
    last_active: float | None = None       # time.time()；最後一題答完的時間
    context_tokens: int = 0                # 上一題最後一個請求的輸入總量

    def reset(self):
        self.conversation = core.Conversation()
        self.last_active = None
        self.context_tokens = 0


def reset_reason(state, now, idle_sec=IDLE_RESET_SEC, max_context=CONTEXT_RESET_TOKENS):
    """要自動開新對話的原因；不用重置回傳 None。對話是空的就不用重置。"""
    if not state.conversation.messages:
        return None
    if state.last_active is not None and now - state.last_active > idle_sec:
        return f"閒置超過 {idle_sec // 3600} 小時"
    if state.context_tokens > max_context:
        return f"對話超過 {max_context // 10000} 萬 token"
    return None


class ProgressThrottle:
    """進度訊息的頻率限制：距離上次改訊息不到 interval 秒就跳過。"""

    def __init__(self, interval=PROGRESS_INTERVAL, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self.last = clock()

    def ready(self):
        now = self.clock()
        if now - self.last < self.interval:
            return False
        self.last = now
        return True


def tool_label(record):
    args = record.get("input")
    if record.get("name") == "classic_commentary":
        return "查倪師對經文的講解"
    if record.get("name") == "search" and isinstance(args, dict) and args.get("query"):
        if args.get("kind") == "classic":
            return f"搜尋經典：{args['query']}"
        return f"搜尋：{args['query']}"
    if record.get("name") == "read_context":
        return "讀前後文"
    return str(record.get("name") or "工具")


def progress_text(labels, notices=()):
    lines = list(notices) + ["查詢中…"]
    queries = [label[len("搜尋："):] for label in labels if label.startswith("搜尋：")]
    if queries:
        shown = queries[-PROGRESS_QUERIES:]
        more = f"（共 {len(queries)} 次）" if len(queries) > len(shown) else ""
        lines.append("已搜尋：" + "、".join(shown) + more)
    classics = [label[len("搜尋經典："):] for label in labels if label.startswith("搜尋經典：")]
    if classics:
        shown = classics[-PROGRESS_QUERIES:]
        more = f"（共 {len(classics)} 次）" if len(classics) > len(shown) else ""
        lines.append("已搜尋經典：" + "、".join(shown) + more)
    commentaries = labels.count("查倪師對經文的講解")
    if commentaries:
        lines.append(f"已查倪師對經文的講解 {commentaries} 次")
    reads = sum(1 for label in labels if label == "讀前後文")
    if reads:
        lines.append(f"已讀前後文 {reads} 次")
    return "\n".join(lines)


# ---------- 錯誤與答案 ----------

def error_message(problem, model=core.DEFAULT_MODEL):
    """例外 → 給使用者看的說明。不放原始錯誤內容（可能含金鑰或請求內容）。"""
    status = getattr(problem, "status_code", None) or getattr(problem, "code", None)
    gemini = model.startswith("gemini-")
    service = "Gemini" if gemini else "Claude"
    try:
        import anthropic
    except ImportError:  # pragma: no cover — 部署環境一定有
        anthropic = None
    if isinstance(problem, core.MissingApiKey):
        return "缺 ANTHROPIC_API_KEY，寫進 .env 後重啟 bot。"
    if gemini:
        import httpx
        if isinstance(problem, httpx.TimeoutException):
            return "Gemini 逾時，再問一次。"
        if isinstance(problem, httpx.ConnectError):
            return "連不上 Gemini（網路問題），等一下再問。"
    if not gemini and anthropic is not None and isinstance(problem, anthropic.APITimeoutError):
        return "Claude 逾時，再問一次。"
    if not gemini and anthropic is not None and isinstance(problem, anthropic.APIConnectionError):
        return "連不上 Claude（網路問題），等一下再問。"
    if status == 429:
        return f"{service} 流量限制（HTTP 429），等一兩分鐘再問。"
    if status in (401, 403):
        if gemini:
            return f"Gemini 權限錯誤（HTTP {status}），檢查服務帳號的 Vertex AI 權限。"
        return f"Claude 金鑰被拒（HTTP {status}），檢查 .env 的 ANTHROPIC_API_KEY。"
    if status == 400:
        return f"{service} 不接受這次請求（HTTP 400），/new 開新對話再問。"
    if isinstance(status, int) and status >= 500:
        return f"{service} 伺服器忙碌（HTTP {status}），等一下再問。"
    if status:
        return f"{service} 錯誤（HTTP {status}），等一下再問。"
    return f"出錯了（{type(problem).__name__}），再問一次；一直失敗就看 bot 的 log。"


def answer_notes(result):
    """答案區塊後的提示。"""
    notes = []
    if any(call.get("mode") == "bm25" for call in result.tool_calls):
        notes.append(BM25_NOTE)
    if result.fallback:
        notes.append(f"（這題改由 {model_name(result.model)} 回答）")
    if any("截斷" in note for note in result.notes):
        notes.append("（回答太長，被截斷）")
    if any("工具呼叫達到上限" in note for note in result.notes):
        notes.append("（查詢輪數到上限，可能沒查完）")
    return notes


def answer_text(result, displayed=None):
    """拒答與純文字用途；一般答案用 answer_messages 包成引用區塊。"""
    text = result.text if displayed is None else displayed
    if result.refused:
        text = text.replace("互動模式輸入 /new", "輸入 /new")
    return "\n\n".join([text, *answer_notes(result)])


def _plain_length(markup):
    return utf16_len(html_to_plain(markup))


def _quote(title, body, expandable=True):
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{tag}<b>{html.escape(title, quote=False)}</b>\n{body}</blockquote>"


def _short_question(question):
    return question[:30] + ("…" if len(question) > 30 else "")


_SUMMARY_LINE = re.compile(r"^(?:總結[：:]|【總結】)(.*)$")


def _answer_title_and_body(question, displayed, model):
    """只調整 Telegram 顯示；不修改 Answer、對話歷史或 JSONL。"""
    lines = displayed.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is not None:
        found = _SUMMARY_LINE.match(lines[first].strip())
        if found and found.group(1).strip():
            summary = found.group(1).strip()
            title = summary[:60]
            if len(summary) > 60:
                for citation in re.finditer(r"/s_[0-9a-f]{4,16}", summary):
                    if citation.start() < 60 < citation.end():
                        title = summary[:citation.start()].rstrip()
                        break
                title += "…"
            body = "\n".join(lines[:first] + lines[first + 1:]).strip("\n")
            return title, body
    log.info("答案缺總結行：%s", model)
    return _short_question(question), displayed


def _trim_thinking(thinking, limit):
    """思考摘要太長時截斷；省略數量以原始字數計。"""
    if utf16_len(thinking) <= limit:
        return thinking
    kept = min(len(thinking), max(0, limit - 32))
    while kept:
        suffix = f"…（思考過程較長，後面省略約 {len(thinking) - kept} 字）"
        if utf16_len(thinking[:kept] + suffix) <= limit:
            return thinking[:kept] + suffix
        kept -= 1
    return f"…（思考過程較長，後面省略約 {len(thinking)} 字）"


def answer_messages(question, answer, displayed, notices=()):
    """每則都含完整引用標籤；長答案每則重新包一個回答引用區塊。"""
    title = f"{model_name(answer.model)}｜思考 {math.floor(answer.elapsed_sec + 0.5)} 秒｜{money(answer.cost_usd)}"
    thinking = answer.thinking or "（這題沒有思考內容）"
    answer_title, answer_body = _answer_title_and_body(question, displayed, answer.model)
    answer_html = to_html(answer_body)
    extras = "\n\n".join(html.escape(note, quote=False) for note in (*notices, *answer_notes(answer)))
    thinking_block = _quote(title, to_html(thinking))
    answer_block = _quote(answer_title, answer_html, False)
    blocks = thinking_block + "\n" + answer_block
    if _plain_length(blocks) <= ANSWER_LIMIT:
        combined = blocks + ("\n\n" + extras if extras else "")
        return [combined] if _plain_length(combined) <= ANSWER_LIMIT else [
            blocks, *split_html(extras, limit=ANSWER_LIMIT)]

    # 分則時思考獨立一則，回答依內容拆分，標題和提示也算進純文字上限。
    thinking_limit = ANSWER_LIMIT - utf16_len(title) - 2
    messages = [_quote(title, to_html(_trim_thinking(thinking, thinking_limit)))]
    first_limit = ANSWER_LIMIT - utf16_len(answer_title) - 2
    continuation_limit = ANSWER_LIMIT - utf16_len("（續）") - 2
    chunks = split_html(answer_html, limit=min(first_limit, continuation_limit)) or ["（空白）"]
    for index, chunk in enumerate(chunks):
        heading = answer_title if index == 0 else "（續）"
        messages.append(_quote(heading, chunk, False))
    if extras:
        candidate = messages[-1] + "\n\n" + extras
        if _plain_length(candidate) <= ANSWER_LIMIT:
            messages[-1] = candidate
        else:
            messages.extend(split_html(extras, limit=ANSWER_LIMIT))
    return messages


_SOURCE_CODE = re.compile(r"（編號 ([0-9a-f]{4,16})）")


def clickable_citations(text, store):
    """只把索引中確實唯一的編號變成 Telegram 可點的 /s_ 指令。須在 worker 執行緒呼叫。"""
    valid = {}

    def replace(match):
        code = match.group(1)
        if code not in valid:
            matches = store.find_prefix(code, limit=2)
            valid[code] = len(matches) == 1
        return f" /s_{code}" if valid[code] else match.group()

    return _SOURCE_CODE.sub(replace, text)


_CLASSIC_SOURCE = re.compile(r"^jicheng:(.+)#\d+(?:-.*)?$")


def display_source(source):
    found = _CLASSIC_SOURCE.fullmatch(source)
    return f"中醫笈成《{found.group(1)}》" if found else display_source_path(source)


def format_source(target, previous, following):
    """/source 的完整 HTML；傳送時由 source_messages 保持各引用區塊完整。"""
    return "\n\n".join(_source_blocks(target, previous, following))


def _source_blocks(target, previous, following):
    """依原文順序組出資訊、前段、本段、後段。"""
    escape = lambda value: html.escape(str(value), quote=False)  # noqa: E731
    source = target["source"]
    shown_source = display_source(source)
    lines = [f"<b>原文（段落 {escape(target['id'])}）</b>",
             f"出處：{escape(citation(target))}",
             f"原始標題：{escape(original_title(target) or '（無）')}",
             f"檔案：{escape(shown_source)}"]
    if shown_source != source and "倪海厦08年医案959篇" in source:
        lines.append(f"Drive 上的原檔名：{escape(source)}")
    if target.get("section"):
        lines.append(f"章節：{escape(target['section'])}")
    blocks = ["\n".join(lines)]
    for label, record in ([("前一段", item) for item in previous] + [("這一段", target)]
                          + [("後一段", item) for item in following]):
        blocks.append(_quote(label, escape(record['text'].strip()), label != "這一段"))
    if not previous and not following:
        blocks.append("（沒有前後段）")
    return blocks


def _source_text_chunks(body, title):
    """在換行處切原文；超長單行才逐字切，保留 Telegram 的純文字餘裕。"""
    limit = ANSWER_LIMIT - utf16_len(title) - 1
    chunks, current = [], ""
    for line in body.split("\n"):
        candidate = current + "\n" + line if current else line
        if utf16_len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while utf16_len(line) > limit:
            size, end = 0, 0
            for char in line:
                width = utf16_len(char)
                if size + width > limit:
                    break
                size += width
                end += 1
            piece = line[:end]
            chunks.append(piece)
            line = line[end:]
        current = line
    if current or not chunks:
        chunks.append(current)
    return chunks


def source_messages(target, previous, following):
    """一則放得下就合併；否則資訊與每個完整引用區塊分則。"""
    blocks = _source_blocks(target, previous, following)
    combined = "\n\n".join(blocks)
    if _plain_length(combined) <= ANSWER_LIMIT:
        return [combined]
    messages = split_html(blocks[0], limit=ANSWER_LIMIT)
    for label, record in ([("前一段", item) for item in previous] + [("這一段", target)]
                          + [("後一段", item) for item in following]):
        body = record["text"].strip()
        chunks = _source_text_chunks(body, label + "（續）")
        for index, chunk in enumerate(chunks):
            heading = label if index == 0 else label + "（續）"
            messages.append(_quote(heading, html.escape(chunk, quote=False), label != "這一段"))
    if not previous and not following:
        messages.append(blocks[-1])
    return messages


def help_text(model, budget):
    return f"""直接打問題，病例寫越詳細越好。

/model 換模型
/new 開新對話
/cost 看花費
/source 編號 看原文
點出處後面的 /s_… 看原文

目前模型：{model_name(model)}
每日上限：{money(budget)}"""


# ---------- 問答執行緒 ----------

class AnswererPool:
    """在 worker 執行緒裡建立、使用：一個共用的 Searcher（SQLite＋向量 memmap），每個模型一個 Answerer。"""

    def __init__(self, make_searcher, make_answerer):
        self.make_searcher = make_searcher
        self.make_answerer = make_answerer
        self.searcher = None
        self.answerers = {}

    def get(self, model):
        if self.searcher is None:
            self.searcher = self.make_searcher()
        if model not in self.answerers:
            self.answerers[model] = self.make_answerer(model, self.searcher)
        return self.answerers[model]

    @property
    def store(self):
        if self.searcher is None:
            self.searcher = self.make_searcher()
        return self.searcher.store

    def close(self):
        for answerer in self.answerers.values():
            answerer.close()
        if self.searcher is not None:
            self.searcher.close()


@dataclass
class JobResult:
    kind: str                   # answer、budget、error
    text: str
    notices: list = field(default_factory=list)
    answer: object = None


class Progress:
    """worker 執行緒回報進度用：把 edit 丟回 asyncio 的事件迴圈；答案送出前先等這些 edit 做完。"""

    def __init__(self, core_bot, bot, chat_id, message_id, loop, throttle):
        self.core_bot, self.bot = core_bot, bot
        self.chat_id, self.message_id = chat_id, message_id
        self.loop, self.throttle = loop, throttle
        self.labels, self.notices, self.futures = [], [], []

    def _schedule(self, text):
        if self.message_id is None:
            return
        coroutine = self.core_bot.edit_plain(self.bot, self.chat_id, self.message_id, text)
        self.futures.append(asyncio.run_coroutine_threadsafe(coroutine, self.loop))

    def started(self, queued):
        """輪到這題時：排過隊（或自動開了新對話）就把訊息改成「查詢中…」，並重新計算頻率限制。"""
        if queued or self.notices:
            self._schedule(progress_text(self.labels, self.notices))
            self.throttle.last = self.throttle.clock()

    def on_tool(self, record):
        self.labels.append(tool_label(record))
        if self.throttle.ready():
            self._schedule(progress_text(self.labels, self.notices))

    async def drain(self):
        if self.futures:
            await asyncio.gather(*(asyncio.wrap_future(f) for f in self.futures), return_exceptions=True)


def _is_bad_request(problem):
    return any(cls.__name__ == "BadRequest" for cls in type(problem).__mro__)


def _retry_seconds(problem):
    value = getattr(problem, "retry_after", None)
    if value is None:
        return None
    if isinstance(value, timedelta):
        value = value.total_seconds()
    return min(float(value), 30.0)


def log_entity_counts(message):
    """只記 Telegram 回傳的 entity 類型與數量，不記答案內容。"""
    counts = Counter(getattr(entity, "type", "unknown") for entity in
                     (getattr(message, "entities", None) or ()))
    summary = "、".join(f"{kind} {count}" for kind, count in sorted(counts.items())) or "無"
    log.info("答案訊息 entity：%s", summary)


class BotCore:
    def __init__(self, allowed, pool, *, daily_budget=DEFAULT_DAILY_BUDGET, log_dir=core.DEFAULT_LOG_DIR,
                 default_model=core.DEFAULT_MODEL, clock=time.time, monotonic=time.monotonic,
                 now=lambda: datetime.now(timezone.utc), progress_interval=PROGRESS_INTERVAL,
                 typing_interval=TYPING_INTERVAL):
        self.allowed = frozenset(allowed)
        self.pool = pool                    # 只在 worker 執行緒裡使用
        self.daily_budget = daily_budget
        self.log_path = Path(log_dir) / core.LOG_NAME
        self.default_model = default_model
        self.clock, self.monotonic, self.now = clock, monotonic, now
        self.progress_interval = progress_interval
        self.typing_interval = typing_interval
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="haixia-answer")
        self.chats = {}
        self.pending = 0                    # 已交給 worker、還沒做完的工作數（含正在做的）
        self._closed = False

    def state(self, chat_id):
        if chat_id not in self.chats:
            self.chats[chat_id] = ChatState(model=self.default_model)
        return self.chats[chat_id]

    def is_allowed(self, user_id):
        return user_id is not None and user_id in self.allowed

    def close(self):
        """停止時等 worker 的工作完成，再由同一執行緒關掉 SQLite 等資源。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.executor.submit(self.pool.close).result()
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)

    # ----- 送訊息 -----

    async def _call(self, method, **kwargs):
        """呼叫 Telegram；被限速（RetryAfter）或網路逾時時重試兩次。"""
        for attempt in range(3):
            try:
                return await method(**kwargs)
            except Exception as problem:  # noqa: BLE001
                wait = _retry_seconds(problem)
                if wait is None and type(problem).__name__ in ("TimedOut", "NetworkError"):
                    wait = 2.0
                if wait is None or attempt == 2:
                    raise
                await asyncio.sleep(wait)

    async def send_html(self, bot, chat_id, text, *, answer_message=False):
        """送一則 HTML 訊息；HTML 解析失敗就改送純文字。送不出去回傳 None（只記 log）。"""
        try:
            sent = await self._call(bot.send_message, chat_id=chat_id, text=text, parse_mode="HTML")
            if answer_message:
                log_entity_counts(sent)
            return sent
        except Exception as problem:  # noqa: BLE001
            if not _is_bad_request(problem):
                log.error("送訊息失敗：%s", type(problem).__name__)
                return None
            log.warning("HTML 被 Telegram 拒絕：%s", type(problem).__name__)
        try:
            sent = await self._call(bot.send_message, chat_id=chat_id, text=html_to_plain(text))
            if answer_message:
                log_entity_counts(sent)
            return sent
        except Exception as problem:  # noqa: BLE001
            log.error("送純文字訊息也失敗：%s", type(problem).__name__)
            return None

    async def edit_html(self, bot, chat_id, message_id, text, *, answer_message=False):
        """把訊息改成 HTML；解析失敗改用純文字。成功回傳 True。"""
        try:
            sent = await self._call(bot.edit_message_text, chat_id=chat_id, message_id=message_id, text=text,
                                    parse_mode="HTML")
            if answer_message:
                log_entity_counts(sent)
            return True
        except Exception as problem:  # noqa: BLE001
            if not _is_bad_request(problem):
                log.warning("改訊息失敗：%s", type(problem).__name__)
                return False
            log.warning("HTML 被 Telegram 拒絕：%s", type(problem).__name__)
        try:
            sent = await self._call(bot.edit_message_text, chat_id=chat_id, message_id=message_id,
                                    text=html_to_plain(text))
            if answer_message:
                log_entity_counts(sent)
            return True
        except Exception as problem:  # noqa: BLE001
            log.warning("改成純文字也失敗：%s", type(problem).__name__)
            return False

    async def edit_plain(self, bot, chat_id, message_id, text):
        try:
            await self._call(bot.edit_message_text, chat_id=chat_id, message_id=message_id, text=text)
        except Exception as problem:  # noqa: BLE001 — 進度訊息改不成功不影響答案
            log.info("進度訊息沒有更新：%s", type(problem).__name__)

    async def send_long(self, bot, chat_id, text, status=None):
        """送出（可能很長的）HTML：第一則改寫 status 訊息（有的話），其餘接著送。"""
        chunks = split_html(text) or ["（空白）"]
        first, rest = chunks[0], chunks[1:]
        if status is None or not await self.edit_html(bot, chat_id, status.message_id, first):
            await self.send_html(bot, chat_id, first)
        for chunk in rest:
            await self.send_html(bot, chat_id, chunk)

    async def send_answer_messages(self, bot, chat_id, messages, status=None):
        first, *rest = messages
        if status is None or not await self.edit_html(bot, chat_id, status.message_id, first,
                                                       answer_message=True):
            await self.send_html(bot, chat_id, first, answer_message=True)
        for chunk in rest:
            await self.send_html(bot, chat_id, chunk, answer_message=True)

    async def _typing(self, bot, chat_id):
        while True:
            await asyncio.sleep(self.typing_interval)
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as problem:  # noqa: BLE001
                log.info("typing 失敗：%s", type(problem).__name__)

    # ----- 訊息進來 -----

    async def handle_message(self, bot, chat_id, user_id, text):
        if not self.is_allowed(user_id):
            log.warning("拒絕 user_id=%s", user_id)
            return
        if text is None:
            await self.send_html(bot, chat_id, "只接受文字訊息。")
            return
        text = text.strip()
        if not text:
            return
        command = parse_command(text)
        if command is not None:
            await self.handle_command(bot, chat_id, command)
        else:
            await self.handle_question(bot, chat_id, text)

    async def handle_command(self, bot, chat_id, command):
        state = self.state(chat_id)
        if command.name in ("start", "help"):
            await self.send_html(bot, chat_id, help_text(state.model, self.daily_budget))
        elif command.name == "new":
            state.reset()
            await self.send_html(bot, chat_id, "已開新對話。")
        elif command.name == "model":
            if command.arg:
                await self.send_html(bot, chat_id, self.switch_model(state, command.arg))
            else:
                menu_text, rows = model_menu(state.model)
                await self._call(bot.send_message, chat_id=chat_id, text=menu_text,
                                 reply_markup=model_keyboard(rows))
        elif command.name == "cost":
            summary = read_costs(self.log_path, self.now())
            await self.send_html(bot, chat_id, html.escape(cost_message(summary, self.daily_budget), quote=False))
        elif command.name == "source":
            await self.handle_source(bot, chat_id, command.arg)
        else:
            await self.send_html(bot, chat_id, f"不認得 /{html.escape(command.raw)}，/help 看指令。")

    def switch_model(self, state, arg):
        model = resolve_model(arg)
        if model is None:
            return f"不認得「{html.escape(arg)}」，可用：" + "、".join(MODELS)
        if model == state.model:
            return f"已經是 {model_name(model)}。"
        state.model = model
        state.reset()
        return f"已切換到 {model_name(model)}，開新對話。"

    async def handle_model_callback(self, bot, query, user_id):
        """依按鈕使用者 ID 授權；每個 callback 都先 answer 以停止轉圈。"""
        if not self.is_allowed(user_id):
            log.warning("拒絕 model callback user_id=%s", user_id)
            await query.answer()
            return
        data = query.data
        if not isinstance(data, str) or (data not in ("m:c", "m:g", "m:b") and data not in MODEL_CALLBACKS):
            await query.answer(text="選單過期了，重打 /model")
            return
        await query.answer()
        message = query.message
        if message is None:
            return
        chat_id, message_id = message.chat_id, message.message_id
        state = self.state(chat_id)
        if data in MODEL_CALLBACKS:
            model = MODELS[MODEL_CALLBACKS[data]]
            text = (f"已經是 {model_name(model)}。" if model == state.model
                    else self.switch_model(state, MODEL_CALLBACKS[data]))
            markup = None
        else:
            provider = {"m:c": "claude", "m:g": "gemini", "m:b": None}[data]
            text, rows = model_menu(state.model, provider)
            markup = model_keyboard(rows)
        await self._call(bot.edit_message_text, chat_id=chat_id, message_id=message_id,
                         text=text, reply_markup=markup)

    async def handle_source(self, bot, chat_id, arg):
        code = normalize_code(arg)
        if code is None:
            await self.send_html(bot, chat_id, "用法：/source 編號，例如 /source 3e13af")
            return
        ahead = self.pending
        if ahead:
            await self.send_html(bot, chat_id, f"前面還有 {ahead} 題，查完就回。")
        self.pending += 1
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(self.executor, self._source_job, code)
        finally:
            self.pending -= 1
        if isinstance(text, list):
            for message in text:
                await self.send_html(bot, chat_id, message)
        else:
            await self.send_long(bot, chat_id, text)

    def _source_job(self, code):
        """worker 執行緒：用編號（id 前綴）找段落與前後各一段。"""
        try:
            store = self.pool.store
            matches = store.find_prefix(code, limit=6)
            if not matches:
                return f"找不到 {code}。索引更新過的話，舊編號會失效。"
            if len(matches) > 1:
                shown = "\n".join(f"- {html.escape(item.get('short_id') or item['id'][:10])}…："
                                  f"{html.escape(citation(item), quote=False)}" for item in matches[:5])
                more = "（只列前 5 段）" if len(matches) > 5 else ""
                return f"{code} 對應到不只一段{more}，多打幾碼：\n{shown}"
            target, previous, following = store.neighbors(matches[0]["id"], 1, 1)
            return source_messages(target, previous, following)
        except Exception as problem:  # noqa: BLE001
            log.error("查原文失敗：%s", type(problem).__name__)
            return f"查原文出錯（{type(problem).__name__}），再試一次。"

    async def handle_question(self, bot, chat_id, question):
        ahead = self.pending
        self.pending += 1
        typing = None
        progress = None
        try:
            status_text = "查詢中…" if not ahead else f"排隊中，前面還有 {ahead} 題"
            try:
                status = await self._call(bot.send_message, chat_id=chat_id, text=status_text)
            except Exception as problem:  # noqa: BLE001
                log.warning("送「查詢中」失敗：%s", type(problem).__name__)
                status = None
            # 先送出一次 typing；極快完成的題目也要讓使用者看到動作。
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as problem:  # noqa: BLE001
                log.info("typing 失敗：%s", type(problem).__name__)
            typing = asyncio.create_task(self._typing(bot, chat_id))
            loop = asyncio.get_running_loop()
            throttle = ProgressThrottle(self.progress_interval, self.monotonic)
            progress = Progress(self, bot, chat_id, getattr(status, "message_id", None), loop, throttle)
            try:
                result = await loop.run_in_executor(self.executor, self._question_job, chat_id, question,
                                                    progress, bool(ahead))
            except Exception as problem:  # noqa: BLE001 — 例如讀花費紀錄失敗
                log.error("處理問題失敗：%s", type(problem).__name__)
                result = JobResult("error", error_message(problem))
        finally:
            self.pending -= 1
            if typing is not None:
                typing.cancel()
        if progress is not None:
            await progress.drain()
        if result.kind == "answer" and result.answer is not None and not result.answer.refused:
            messages = answer_messages(question, result.answer, result.text, result.notices)
            await self.send_answer_messages(bot, chat_id, messages, status)
        else:
            text = "\n\n".join([*result.notices, result.text])
            await self.send_long(bot, chat_id, to_html(text), status)

    def _question_job(self, chat_id, question, progress, queued):
        """worker 執行緒：自動重置、每日上限、呼叫 Answerer。"""
        state = self.state(chat_id)
        notices = []
        reason = reset_reason(state, self.clock())
        if reason:
            state.reset()
            notices.append(f"（新對話：{reason}）")
        progress.notices = notices
        progress.started(queued)
        summary = read_costs(self.log_path, self.now())
        if summary.today_usd >= self.daily_budget:
            log.warning("已達每日上限：今天 %.4f 美元", summary.today_usd)
            return JobResult("budget", budget_message(summary, self.daily_budget), notices)
        conversation = state.conversation
        try:
            answerer = self.pool.get(state.model)
            result = answerer.ask(conversation, question, on_tool=progress.on_tool)
        except Exception as problem:  # noqa: BLE001 — 一律回友善說明，log 只記類型與狀態碼
            log.error("回答失敗：%s（HTTP %s）", type(problem).__name__,
                      getattr(problem, "status_code", None) or getattr(problem, "code", None))
            state.last_active = self.clock()
            return JobResult("error", error_message(problem, state.model), notices)
        if state.conversation is conversation:     # 回答期間使用者沒有 /new 或換模型
            state.last_active = self.clock()
            state.context_tokens = result.context_tokens
        log.info("回答完成：%s，%d 輪工具，US$%.4f，%.1f 秒%s", result.model, result.rounds, result.cost_usd,
                 result.elapsed_sec, "（拒答）" if result.refused else "")
        displayed = core.display_answer(result.text, conversation)
        displayed = clickable_citations(displayed, self.pool.store)
        return JobResult("answer", answer_text(result, displayed) if result.refused else displayed,
                         notices, result)


# ---------- python-telegram-bot ----------

def build_application(bot_core, token):
    """建立 python-telegram-bot 的 Application（long polling）。只有這裡 import telegram。"""
    from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters

    async def on_message(update, context):
        message, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if message is None or chat is None:
            return
        await bot_core.handle_message(context.bot, chat.id, user.id if user else None, message.text)

    async def on_callback(update, context):
        query = update.callback_query
        if query is not None:
            user = update.effective_user
            await bot_core.handle_model_callback(context.bot, query, user.id if user else None)

    async def on_error(update, context):
        log.error("處理 Telegram 更新時出錯：%s", type(context.error).__name__)

    async def post_init(application):
        try:
            await application.bot.set_my_commands(BOT_COMMANDS)
        except Exception as problem:  # noqa: BLE001
            log.warning("設定指令選單失敗：%s", type(problem).__name__)

    async def post_shutdown(application):
        await asyncio.get_running_loop().run_in_executor(None, bot_core.close)

    application = (ApplicationBuilder().token(token).concurrent_updates(True)
                   .post_init(post_init).post_shutdown(post_shutdown).build())
    # 只處理新訊息（不處理編輯過的訊息、頻道貼文），讓排隊由 BotCore 的 worker 負責
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE, on_message))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_error_handler(on_error)
    return application
