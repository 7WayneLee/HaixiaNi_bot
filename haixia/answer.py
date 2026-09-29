"""第四步：Claude 問答核心——搜尋工具、工具迴圈、多輪對話與費用紀錄。

設計重點（依 claude-api skill 對 Opus 5.5 的指引）：
- 思考不能關（Opus 5.5 送 disabled 會 400），只用 thinking adaptive＋output_config.effort 控制，預設 medium。
- Opus 5.5 不接受強制 tool_choice（any／tool），只用 auto；工具用 strict: true。
  串流時工具另加 eager_input_streaming，伺服器不再驗證輸入，所以執行前自己驗證。
- 對話只附加、不改寫（preserved thinking）：每輪把完整的 response.content 放回歷史，
  system prompt 與工具定義在 Answerer 建立時就固定，之後不動。
- 超過工具輪數上限時，不拿掉工具（會改到快取與思考綁定的前綴），而是改送 tool_choice none，
  並在最後一則工具結果後面附一段說明。
- prompt caching：system 最後一塊加 cache_control（連同前面的工具定義一起快取），
  另開頂層自動快取給持續變長的對話。
- 拒答：先看 stop_reason == "refusal" 再讀內容；Opus 5.5 預設開伺服器端 fallback（"default"）。
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from haixia.search import Searcher, citation

REPO = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT_PATH = REPO / "data" / "system_prompt.md"
ENV_PATH = REPO / ".env"
WORKSPACE_HEADER = "anthropic-workspace-id"
DEFAULT_LOG_DIR = Path.home() / "haixia-bot-logs"
LOG_NAME = "answers.jsonl"

DEFAULT_MODEL = "claude-opus-5-5"
COMPARE_MODEL = "claude-sonnet-5-5"   # 第六步比較用（2026-09-28 推出，與 Sonnet 5 同價）
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 64000          # 串流請求的建議值；思考也算在裡面
MAX_TOOL_ROUNDS = 8
JSON_RETRIES = 2            # 串流中工具輸入的 JSON 完全解析不了時，重送同一個請求的次數

FALLBACK_BETA = "server-side-fallback-2026-07-01"   # fallbacks: "default"
BINDING_BETA = "thinking-binding-controls-2026-08-01"
# 預設開伺服器端 fallback 的模型（skill：Opus 5.5／Opus 5／Fable 5.1 從第一天就開）
FALLBACK_DEFAULT_MODELS = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"})

# 美元／百萬 token。cache_write 是 5 分鐘快取的寫入價（輸入價的 1.25 倍）。
# Opus 5.5 的快取讀取是輸入價的 0.05 倍，Sonnet 5／5.5 是 $0.20，其他是 0.1 倍。可用 load_prices() 以 JSON 覆蓋。
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    # fallback 可能改由下面的模型回答
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
}
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
USAGE_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")

KINDS = {"any": None, "transcript": "transcript", "document": "document", "classic": "classic"}
KIND_NAMES = {"transcript": "逐字稿", "document": "文件", "classic": "經典"}
SCOPE_NAMES = {"any": "全部", **KIND_NAMES}
DEFAULT_K = 8
MAX_K = 10
MAX_CONTEXT = 3
MAX_QUERY_CHARS = 200

LIMIT_NOTE = (f"（系統說明：這一題的工具呼叫已達上限 {MAX_TOOL_ROUNDS} 輪，不能再查。"
              "請只用上面已經查到的資料作答；資料不足的部分請明說，不要編造。）")
REFUSAL_TEXT = ("抱歉，這一題被 Claude 的安全機制擋下，沒有產生回答{category}。"
                "這可能是誤判：可以換個說法再問；如果之後的問題也一直被擋，請開新對話（互動模式輸入 /new）。")

TOOLS = [
    {
        "name": "search",
        "description": (
            "搜尋倪海廈（倪師）的課程逐字稿、文件與經典原文，"
            "混合語意與關鍵字搜尋，回傳最相關段落的 id、出處與全文。"
            "回答任何跟倪師教學或中醫內容有關的問題前都要先用它查；一個問題通常要換不同關鍵字"
            "（方名、藥名、穴位、條文原句、病名、症狀、課名）查好幾次。"
        ),
        "strict": True,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "查詢內容，用正體中文；短而具體的關鍵詞或一句話，例如「桂枝湯 組成」「少陽病提綱」。"},
                "kind": {"type": "string", "enum": list(KINDS),
                         "description": "any＝全部；transcript＝逐字稿；document＝講義等文件；classic＝經典原文。"},
                "k": {"type": "integer", "enum": list(range(1, MAX_K + 1)),
                      "description": f"回傳幾段，1–{MAX_K}，預設 {DEFAULT_K}。"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "classic_commentary",
        "description": "給經典段落 id，讀取已連結的倪師講義及上課逐字稿段落，附講義條號、出處與內文。",
        "strict": True,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object", "properties": {"id": {"type": "string", "description": "經典搜尋回傳的段落 id。"}},
            "required": ["id"], "additionalProperties": False,
        },
    },
    {
        "name": "read_context",
        "description": (
            "讀取某個段落在同一來源（同一集逐字稿或同一份文件）裡前後相鄰的段落。"
            "搜尋命中的段落話講到一半、或需要前後文才能確定意思時使用。"
        ),
        "strict": True,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "search 回傳的段落 id。"},
                "before": {"type": "integer", "enum": list(range(MAX_CONTEXT + 1)),
                           "description": f"要讀前面幾段，0–{MAX_CONTEXT}，預設 1。"},
                "after": {"type": "integer", "enum": list(range(MAX_CONTEXT + 1)),
                          "description": f"要讀後面幾段，0–{MAX_CONTEXT}，預設 1。"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]


# ---------- 金鑰 ----------

class MissingApiKey(RuntimeError):
    pass


def read_env_file(path):
    """讀 .env 的 KEY=VALUE（可有 export 前綴、引號、# 註解）；檔案不存在回傳 {}。

    格式不對的行直接略過，不把內容放進任何訊息（那一行可能就是金鑰）。
    """
    values = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def env_setting(name, environ=None, env_path=None):
    """先用環境變數；沒有（或是空的）才讀 .env（預設 repo 根目錄）。不修改環境變數。沒有設定回傳 None。"""
    environ = os.environ if environ is None else environ
    env_path = ENV_PATH if env_path is None else env_path
    value = (environ.get(name) or "").strip()
    if not value:
        value = (read_env_file(env_path).get(name) or "").strip()
    return value or None


def resolve_api_key(environ=None, env_path=None):
    key = env_setting("ANTHROPIC_API_KEY", environ, env_path)
    if not key:
        where = ENV_PATH if env_path is None else env_path
        raise MissingApiKey(f"找不到 ANTHROPIC_API_KEY：請設定環境變數，或寫進 {where}（ANTHROPIC_API_KEY=…）")
    return key


def resolve_workspace_id(environ=None, env_path=None):
    """組織層級（沒綁定 workspace）的金鑰要另外帶 anthropic-workspace-id；沒設定時回傳 None。"""
    return env_setting("ANTHROPIC_WORKSPACE_ID", environ, env_path)


def redact(text, *secrets):
    """把訊息裡的金鑰等機密換成 ***（保險用：印出或記錄例外訊息前先過一次）。"""
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def client_options(api_key, workspace_id=None):
    """anthropic.Anthropic 的參數：有 workspace id 時用 default_headers 讓每個請求都帶上。"""
    options = {"api_key": api_key}
    if workspace_id:
        options["default_headers"] = {WORKSPACE_HEADER: workspace_id}
    return options


def make_client(api_key, workspace_id=None):
    import anthropic

    return anthropic.Anthropic(**client_options(api_key, workspace_id))


def default_embedder(timeout=10):
    """查詢向量走 Vertex AI（movie-nas 用預設服務帳號，不需要金鑰）。"""
    from haixia import vertex

    api = vertex.GoogleApi(timeout=timeout, max_attempts=2)
    return vertex.EmbeddingClient(api)


# ---------- 費用 ----------

def load_prices(path, base=PRICES):
    """讀 JSON 價格表 {模型: {input, output, cache_read, cache_write}}，覆蓋預設值。"""
    prices = {model: dict(values) for model, values in base.items()}
    for model, values in json.loads(Path(path).read_text(encoding="utf-8")).items():
        missing = [name for name in PRICE_FIELDS if name not in values]
        if missing:
            raise ValueError(f"{model} 的價格缺少 {'、'.join(missing)}")
        prices[model] = {name: float(values[name]) for name in PRICE_FIELDS}
    return prices


def usage_dict(usage):
    return {name: int(getattr(usage, name, 0) or 0) for name in USAGE_FIELDS}


def usage_cost(usage, price):
    """usage dict × 價格（美元／百萬 token）→ 美元。"""
    return (usage["input_tokens"] * price["input"]
            + usage["cache_read_input_tokens"] * price["cache_read"]
            + usage["cache_creation_input_tokens"] * price["cache_write"]
            + usage["output_tokens"] * price["output"]) / 1_000_000


def response_cost(response, requested_model, prices):
    """一次請求的 (usage dict, 美元, 備註清單)。

    有 fallback 時，頂層 usage 只算最後回答的那一次，所以改用 usage.iterations 逐次加總，
    每次依實際執行的模型計價。價格表沒有的模型用請求的模型估算並註明。
    """
    notes = []
    iterations = getattr(response.usage, "iterations", None) or []
    billed = [item for item in iterations if getattr(item, "type", None) in ("message", "fallback_message")]
    if any(item.type == "fallback_message" for item in billed):
        entries = [(getattr(item, "model", None) or requested_model, usage_dict(item)) for item in billed]
    else:
        entries = [(getattr(response, "model", None) or requested_model, usage_dict(response.usage))]
    total = dict.fromkeys(USAGE_FIELDS, 0)
    cost = 0.0
    for model, usage in entries:
        price = prices.get(model)
        if price is None:
            price = prices[requested_model]
            notes.append(f"價格表沒有 {model}，以 {requested_model} 估算")
        cost += usage_cost(usage, price)
        for name in USAGE_FIELDS:
            total[name] += usage[name]
    return total, cost, notes


# ---------- 工具 ----------

class ToolInputError(ValueError):
    pass


def _integer(value, name, low, high, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolInputError(f"{name} 要是 {low}–{high} 的整數")
    return value


def validate_input(name, data):
    """驗證並補上預設值（eager 串流時伺服器不驗證工具輸入）。"""
    if not isinstance(data, dict):
        raise ToolInputError("輸入要是物件")
    allowed = {"search": {"query", "kind", "k"}, "read_context": {"id", "before", "after"},
               "classic_commentary": {"id"}}.get(name)
    if allowed is None:
        raise ToolInputError(f"沒有這個工具：{name}")
    extra = set(data) - allowed
    if extra:
        raise ToolInputError(f"不認得的參數：{'、'.join(sorted(extra))}")
    if name == "search":
        query = data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolInputError("query 不能是空的")
        if len(query) > MAX_QUERY_CHARS:
            raise ToolInputError(f"query 最多 {MAX_QUERY_CHARS} 字")
        kind = data.get("kind", "any")
        if kind not in KINDS:
            raise ToolInputError("kind 只能是 any、transcript、document 或 classic")
        return {"query": query.strip(), "kind": kind, "k": _integer(data.get("k"), "k", 1, MAX_K, DEFAULT_K)}
    chunk_id = data.get("id")
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        raise ToolInputError("id 不能是空的")
    if name == "classic_commentary":
        return {"id": chunk_id.strip()}
    return {"id": chunk_id.strip(),
            "before": _integer(data.get("before"), "before", 0, MAX_CONTEXT, 1),
            "after": _integer(data.get("after"), "after", 0, MAX_CONTEXT, 1)}


def format_record(record, label):
    kind = KIND_NAMES.get(record["kind"], record["kind"])
    return f"{label} id={record['id']}｜{kind}\n出處：{citation(record)}\n{record['text'].strip()}"


def run_search(searcher, args):
    """→ (給 Claude 的文字, 命中清單, 搜尋模式)。"""
    result = searcher.search(args["query"], k=args["k"], kind=KINDS[args["kind"]])
    records = result["results"]
    header = f"查詢「{args['query']}」（範圍：{SCOPE_NAMES[args['kind']]}）"
    if result["mode"] != "hybrid":
        header += "［向量搜尋失敗，這次只用關鍵字搜尋］"
    if not records:
        return header + "：找不到相關段落。可以換關鍵字、同義詞或條文原句再查。", [], result["mode"]
    blocks = [f"{header}，共 {len(records)} 段："]
    blocks += [format_record(record, f"[{number}]") for number, record in enumerate(records, 1)]
    hits = [{"id": record["id"], "citation": citation(record)} for record in records]
    return "\n\n".join(blocks), hits, result["mode"]


def run_read_context(store, args):
    try:
        target, previous, following = store.neighbors(args["id"], args["before"], args["after"])
    except KeyError:
        raise ToolInputError(f"找不到段落 id：{args['id']}") from None
    lines = [f"段落 {target['id']}（{citation(target)}）的前後文，依原本順序排列："]
    if not previous and not following:
        lines.append("同一來源裡沒有相鄰的段落（這一段就是開頭或結尾）。")
    for offset, record in enumerate(previous, -len(previous)):
        lines.append(format_record(record, f"[前 {-offset} 段]"))
    lines.append(f"[命中段落 id={target['id']}：內文見先前的搜尋結果]")
    for offset, record in enumerate(following, 1):
        lines.append(format_record(record, f"[後 {offset} 段]"))
    hits = [{"id": record["id"], "citation": citation(record)} for record in previous + following]
    return "\n\n".join(lines), hits


def run_classic_commentary(store, args, max_chars=9000):
    try:
        records = store.classic_commentary(args["id"])
    except KeyError:
        raise ToolInputError(f"找不到經典段落 id：{args['id']}") from None
    if not records:
        return "這段經典沒有可確認的倪師講義或逐字稿連結。", []
    blocks, hits, used = [], [], 0
    for record in records:
        number = f"（講義第{record['lecture_number']}條）" if record.get("lecture_number") else ""
        block = f"id={record['id']}｜{KIND_NAMES.get(record['kind'], record['kind'])}{number}\n出處：{citation(record)}\n{record['text'].strip()}"
        separator = 2 if blocks else 0
        available = max_chars - used - separator
        if available <= 0:
            break
        if len(block) > available:
            block = block[:max(0, available - 1)] + "…"
        blocks.append(block)
        hits.append({"id": record["id"], "citation": citation(record)})
        used += len(block) + separator
    return "\n\n".join(blocks), hits


# ---------- 對話與回答 ----------

class Conversation:
    """多輪對話的歷史。只附加、不改寫：Answerer 只會在 messages 後面加東西。"""

    def __init__(self):
        self.messages = []
        self.questions = 0


@dataclass
class Answer:
    text: str
    stop_reason: str
    model: str
    refused: bool = False
    tool_calls: list = field(default_factory=list)
    rounds: int = 0
    requests: int = 0
    usage: dict = field(default_factory=lambda: dict.fromkeys(USAGE_FIELDS, 0))
    cost_usd: float = 0.0
    elapsed_sec: float = 0.0
    fallback: bool = False
    notes: list = field(default_factory=list)
    # 最後一個請求的輸入總量（input＋快取讀＋快取寫）＝目前對話的長度，下一題至少要重送這麼多
    context_tokens: int = 0


def history_content(content):
    """把回應的 content 放回歷史前的處理。

    平常原封不動（包括空的 thinking 區塊）。只有中途發生 fallback 時，照規定把最後一個
    fallback 區塊之前的 thinking、redacted_thinking、tool_use、server_tool_use 拿掉。
    """
    content = list(content)
    boundary = max((index for index, block in enumerate(content) if block.type == "fallback"), default=None)
    if not boundary:
        return content
    dropped = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}
    return [block for index, block in enumerate(content) if index > boundary or block.type not in dropped]


class Answerer:
    def __init__(self, index_dir, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT, *, client=None,
                 api_key=None, workspace_id=None, embedder="vertex", system_prompt=None,
                 max_tool_rounds=MAX_TOOL_ROUNDS, max_tokens=MAX_TOKENS, fallback=None,
                 prefix_mismatch="drop_block",
                 log_dir=DEFAULT_LOG_DIR, prices=None, clock=time.monotonic, searcher=None):
        """client：anthropic.Anthropic（或同介面物件）；None 時用 api_key（與 workspace_id）建立。

        embedder："vertex"＝用 Vertex 算查詢向量；None＝只用 BM25；或傳入同介面物件（測試用）。
        fallback：None＝依模型決定（Opus 5.5 等預設開）；True／False 強制。
        prefix_mismatch：思考區塊綁定檢查不符時的處理（"drop_block"／"error"／None＝不送）。
        log_dir：None＝不寫 JSONL。
        searcher：共用現成的 Searcher（Telegram bot 切換模型時用），這時不看 index_dir 與 embedder；
        close() 也不會關掉它，由建立的人負責。
        """
        if effort not in EFFORTS:
            raise ValueError(f"effort 只能是 {'、'.join(EFFORTS)}")
        self.prices = PRICES if prices is None else prices
        if model not in self.prices:
            raise ValueError(f"價格表沒有 {model}，請用 --prices 提供")
        self.model = model
        self.effort = effort
        self.max_tool_rounds = max_tool_rounds
        self.max_tokens = max_tokens
        self.fallback = model in FALLBACK_DEFAULT_MODELS if fallback is None else bool(fallback)
        self.prefix_mismatch = prefix_mismatch
        self.log_dir = None if log_dir is None else Path(log_dir)
        self.clock = clock
        text = system_prompt if system_prompt is not None else SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        # system 與工具在整個 Answerer 的生命週期內固定不變（快取前綴、思考綁定都靠它）
        self.system = [{"type": "text", "text": text.strip(), "cache_control": {"type": "ephemeral"}}]
        self.tools = TOOLS
        self.owns_searcher = searcher is None
        if searcher is None:
            if embedder == "vertex":
                embedder = default_embedder()
            searcher = Searcher(index_dir, embedder)
        self.searcher = searcher
        self.client = client if client is not None else make_client(api_key, workspace_id)

    # ----- 請求 -----

    def request_params(self, messages, allow_tools=True):
        thinking = {"type": "adaptive"}
        betas = []
        params = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "tools": self.tools,
            "tool_choice": {"type": "auto" if allow_tools else "none"},
            "messages": messages,
            "thinking": thinking,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.prefix_mismatch:
            thinking["block_binding"] = {"prefix_mismatch_behavior": self.prefix_mismatch}
            betas.append(BINDING_BETA)
        if self.fallback:
            params["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)
        if betas:
            params["betas"] = betas
        return params

    def _send(self, messages, allow_tools):
        params = self.request_params(messages, allow_tools)
        for attempt in range(JSON_RETRIES + 1):
            try:
                with self.client.beta.messages.stream(**params) as stream:
                    return stream.get_final_message()
            except ValueError:
                # SDK 解析不了串流中的工具輸入 JSON（此時沒有 tool_use_id 可回覆），重送同一個請求。
                # API 錯誤不是 ValueError，會直接往外丟。
                if attempt == JSON_RETRIES:
                    raise

    # ----- 工具 -----

    def _run_tool(self, block, round_number, calls, on_tool):
        record = {"round": round_number, "name": block.name, "input": block.input, "hits": [],
                  "mode": None, "error": None}
        try:
            args = validate_input(block.name, block.input)
            record["input"] = args
            if block.name == "search":
                text, record["hits"], record["mode"] = run_search(self.searcher, args)
            elif block.name == "classic_commentary":
                text, record["hits"] = run_classic_commentary(self.searcher.store, args)
            else:
                text, record["hits"] = run_read_context(self.searcher.store, args)
            result = {"type": "tool_result", "tool_use_id": block.id, "content": text}
        except Exception as problem:  # noqa: BLE001 — 工具錯誤一律回給 Claude，不丟掉
            if isinstance(problem, ToolInputError):
                message = str(problem)
            else:
                message = f"工具執行失敗（{type(problem).__name__}）：{problem}"
            if not isinstance(block.input, dict):
                message += "；收到的輸入：" + json.dumps(block.input, ensure_ascii=False, default=str)[:300]
            record["error"] = message
            result = {"type": "tool_result", "tool_use_id": block.id, "content": message, "is_error": True}
        calls.append(record)
        if on_tool:
            on_tool(record)
        return result

    # ----- 問答 -----

    def ask(self, conversation, question, on_tool=None):
        """問一題（可接在之前的對話後面）。回傳 Answer；API 錯誤會往外丟（仍會記 log）。"""
        started = self.clock()
        answer = Answer(text="", stop_reason="", model=self.model)
        messages = conversation.messages
        messages.append({"role": "user", "content": question})
        conversation.questions += 1
        error = None
        try:
            self._loop(messages, answer, on_tool)
        except Exception as problem:
            error = problem
            raise
        finally:
            answer.elapsed_sec = round(self.clock() - started, 3)
            self._log(question, answer, error)
        return answer

    def _loop(self, messages, answer, on_tool):
        allow_tools = True
        while True:
            response = self._send(messages, allow_tools)
            answer.requests += 1
            usage, cost, notes = response_cost(response, self.model, self.prices)
            last = usage_dict(response.usage)
            answer.context_tokens = (last["input_tokens"] + last["cache_read_input_tokens"]
                                     + last["cache_creation_input_tokens"])
            for name in USAGE_FIELDS:
                answer.usage[name] += usage[name]
            answer.cost_usd += cost
            answer.notes += [note for note in notes if note not in answer.notes]
            answer.model = getattr(response, "model", None) or self.model
            answer.stop_reason = response.stop_reason
            if any(getattr(item, "type", None) == "fallback_message"
                   for item in getattr(response.usage, "iterations", None) or []):
                answer.fallback = True
            dropped = getattr(response, "input_transformations", None) or []
            if dropped:
                answer.notes.append(f"API 丟掉了 {len(dropped)} 個思考區塊（input_transformations）")

            if response.stop_reason == "refusal":
                # 拒答（可能只有部分輸出）：不放回歷史、不執行任何工具
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                answer.refused = True
                answer.text = REFUSAL_TEXT.format(category=f"（類別：{category}）" if category else "")
                return

            content = history_content(response.content)
            messages.append({"role": "assistant", "content": content})
            tool_uses = [block for block in content if block.type == "tool_use"]

            if tool_uses and response.stop_reason == "tool_use" and allow_tools:
                answer.rounds += 1
                results = [self._run_tool(block, answer.rounds, answer.tool_calls, on_tool)
                           for block in tool_uses]
                if answer.rounds >= self.max_tool_rounds:
                    allow_tools = False
                    answer.notes.append(f"工具呼叫達到上限 {self.max_tool_rounds} 輪，要求直接作答")
                    results.append({"type": "text", "text": LIMIT_NOTE})
                messages.append({"role": "user", "content": results})
                continue

            if tool_uses:
                # 被截斷（max_tokens）等情況下留下的工具呼叫：不執行，但補上結果，讓歷史保持合法
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                     "content": "回應在這個工具呼叫完成前就停止了，沒有執行。"} for block in tool_uses]})
            answer.text = "".join(block.text for block in content if block.type == "text").strip()
            if response.stop_reason == "max_tokens":
                answer.notes.append("回答超過 max_tokens 被截斷")
            if not answer.text:
                answer.text = "（這次沒有產生文字回答，請再問一次或換個說法。）"
            return

    # ----- 紀錄 -----

    def _log(self, question, answer, error):
        if self.log_dir is None:
            return
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": self.model, "served_model": answer.model, "effort": self.effort,
            "fallback_enabled": self.fallback, "fallback_ran": answer.fallback,
            "question": question, "answer": answer.text, "stop_reason": answer.stop_reason,
            "refused": answer.refused, "rounds": answer.rounds, "requests": answer.requests,
            "tool_calls": answer.tool_calls, "usage": answer.usage,
            "cost_usd": round(answer.cost_usd, 6), "elapsed_sec": answer.elapsed_sec,
            "notes": answer.notes,
            # 只記錯誤類型與 HTTP 狀態碼，不記訊息內容
            "error": None if error is None else {
                "type": type(error).__name__, "status": getattr(error, "status_code", None)},
        }
        try:
            self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(self.log_dir / LOG_NAME, "a", encoding="utf-8") as log:
                log.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError as problem:
            answer.notes.append(f"寫入 log 失敗：{type(problem).__name__}")

    def close(self):
        if self.owns_searcher:
            self.searcher.close()
