"""第四步：Claude 問答核心——搜尋工具、工具迴圈、多輪對話與費用紀錄。

設計重點（依 claude-api skill 對 Opus 5.5 與 Fable 5.2 的指引）：
- 思考不能關（Opus 5.5、Fable 5.2 送 disabled 或 budget_tokens 都會 400），只用 thinking adaptive＋
  output_config.effort 控制；預設 effort 依模型：Opus 5.5 medium、Fable 5.2 high（見 MODEL_EFFORTS）。
- Opus 5.5、Fable 5.2 都不接受強制 tool_choice（any／tool），只用 auto（或 none）；工具用 strict: true。
  串流時工具另加 eager_input_streaming，伺服器不再驗證輸入，所以執行前自己驗證。
- 對話只附加、不改寫（preserved thinking；Fable 5.2 對每個帳號都檢查）：每輪把完整的 response.content 放回歷史，
  system prompt 與工具定義在 Answerer 建立時就固定，之後不動。
- 超過工具輪數上限時，不拿掉工具（會改到快取與思考綁定的前綴），而是改送 tool_choice none，
  並在最後一則工具結果後面附一段說明。
- prompt caching：system 最後一塊加 cache_control（連同前面的工具定義一起快取），
  另開頂層自動快取給持續變長的對話。
- 拒答：先看 stop_reason == "refusal" 再讀內容；Opus 5.5、Fable 5.2 預設開伺服器端 fallback（"default"）。
"""

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import NamedTuple
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
FABLE_MODEL = "claude-fable-5-2"      # 最強的公開模型，價格是 Opus 5.5 的兩倍；需要 30 天資料保存
CLAUDE_MODELS = (DEFAULT_MODEL, COMPARE_MODEL, FABLE_MODEL)   # bot 選單與 ask.py 說明列的 Claude 模型
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "medium"
# 各模型的預設 effort（沒列的用 DEFAULT_EFFORT）。Fable 5.2 官方建議從 high 開始，再往上收益不穩定。
MODEL_EFFORTS = {FABLE_MODEL: "high"}
MAX_TOKENS = 64000          # 串流請求的建議值；思考也算在裡面
MAX_TOOL_ROUNDS = 8
JSON_RETRIES = 2            # 串流中工具輸入的 JSON 完全解析不了時，重送同一個請求的次數

FALLBACK_BETA = "server-side-fallback-2026-07-01"   # fallbacks: "default"
BINDING_BETA = "thinking-binding-controls-2026-08-01"
# 預設開伺服器端 fallback 的模型（skill：Opus 5.5／Opus 5／Fable 5.1／Fable 5.2 從第一天就開）。
# Fable 5.2 拒答時只會改由 Opus 4.8 或 Opus 5 回答（兩者都在 PRICES 裡）。
FALLBACK_DEFAULT_MODELS = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", FABLE_MODEL})

# 美元／百萬 token。cache_write 是 5 分鐘快取的寫入價（輸入價的 1.25 倍）。
# 快取讀取（2026-11-16 依官方價格頁確認）：Fable 5.2 是輸入價的 0.025 倍（$0.20），Opus 5.5、Sonnet 5.5
# 是 0.05 倍（$0.20、$0.10），Sonnet 5 是 $0.20，其他是 0.1 倍。可用 load_prices() 以 JSON 覆蓋。
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50},
    "claude-fable-5-2": {"input": 8.00, "output": 40.00, "cache_read": 0.20, "cache_write": 10.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    # fallback 可能改由下面的模型回答
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    # Gemini：Vertex AI global 端點的價格（2026-11-16 依官方價格頁確認）。只有隱含快取，
    # 不收快取寫入費，cache_write 跟輸入價一樣只是佔位（cache_creation_input_tokens 永遠是 0）。
    # 4 Hydrogen：2027-01-12 起開放 TTL 快取，寫入 $1.875，這裡用不到。
    "gemini-4-hydrogen": {"input": 1.50, "output": 7.50, "cache_read": 0.075, "cache_write": 1.50},
    # 4 Argon：到 2026-11-30 是促銷價（以 5 成 credits 回饋），之後見 PRICE_CHANGES。
    "gemini-4-argon": {"input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.00},
    # 3.8 Flash：優惠價到 2026-12-31，之後見 PRICE_CHANGES。
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75, "cache_read": 0.075, "cache_write": 0.75},
}
# 已排定的調價：模型 → ((生效日（UTC 日期）, 價格), ...)，依日期由舊到新。只套用在預設價格表上。
PRICE_CHANGES = {
    "gemini-4-argon": ((date(2026, 12, 1),
                        {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 4.00}),),
    "gemini-3.8-flash": ((date(2027, 1, 1),
                          {"input": 1.50, "output": 7.50, "cache_read": 0.15, "cache_write": 1.50}),),
}


class GeminiModel(NamedTuple):
    """bot 提供的一個 Gemini 模型。thinking 是預設思考程度（default＝不指定，用模型自己的預設）。"""
    id: str
    alias: str                  # /model 的文字捷徑
    name: str                   # 顯示名稱
    callback: str               # /model 選單按鈕的 callback data（要短、固定）
    thinking: str = "default"
    max_output_tokens: int = 8192   # 思考 token 也算在內


# Gemini 陣容的唯一來源（answer_gemini、telegram_bot、ask.py 都讀這裡），選單依這個順序。
# 2026-11-16 依 Vertex 模型頁確認，三個都在 location=global 實測過 generateContent 與工具呼叫。
# - 4 Hydrogen：官方指定的 3.8 Flash 後繼（快、便宜那一線），預設思考 HIGH。
# - 4 Argon：官方指定的 3.1 Pro 後繼之一，預設思考 MEDIUM。另一個 4 Carbon 是「Google Frontier Model」，
#   所有提示與回答都會被記錄最多 30 天、可能有人工審查，醫案有病人資料，所以不用。
# - 3.8 Flash：優惠價到 2026-12-31，比 Hydrogen 便宜一半；2027 起同價，就可以拿掉。
# Gemini 4 非串流單次輸出上限 32,768（Hydrogen 65,536），思考較多，上限放寬到 16,384。
GEMINI_LINEUP = (
    GeminiModel("gemini-4-hydrogen", "gemini-flash", "Gemini 4 Hydrogen", "m:f", max_output_tokens=16384),
    GeminiModel("gemini-4-argon", "gemini-pro", "Gemini 4 Argon", "m:p", max_output_tokens=16384),
    GeminiModel("gemini-3.8-flash", "gemini-3.8-flash", "Gemini 3.8 Flash", "m:3"),
)
GEMINI_MODELS = {item.id: item for item in GEMINI_LINEUP}
# 停用的模型 id → 顯示名稱；舊對話或 /model 指到它們時改回預設模型並說明。
RETIRED_MODELS = {"gemini-3.1-pro-preview": "Gemini 3.1 Pro"}
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
        "description": ("給經典段落 id，讀取已連結的倪師講義及上課逐字稿段落，附講義條號、出處與內文。"
                        "也可以傳出處裡的編號（例如 /s_a1b2c3）。"),
        "strict": True,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object", "properties": {"id": {"type": "string", "description": "經典段落 id，或至少 4 碼的十六進位出處編號（可帶 /s_ 或 s_）。"}},
            "required": ["id"], "additionalProperties": False,
        },
    },
    {
        "name": "read_context",
        "description": (
            "讀取某個段落在同一來源（同一集逐字稿或同一份文件）裡前後相鄰的段落。"
            "搜尋命中的段落話講到一半、或需要前後文才能確定意思時使用。"
            "也可以傳出處裡的編號（例如 /s_a1b2c3）。"
        ),
        "strict": True,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "段落 id，或至少 4 碼的十六進位出處編號（可帶 /s_ 或 s_）。"},
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
        if value.startswith(("\"", "'")):
            end = value.find(value[0], 1)
            if end > 0 and (not value[end + 1:].strip() or value[end + 1:].lstrip().startswith("#")):
                value = value[1:end]
        else:
            # 和 dotenv 一樣：空白後面的 # 才是註解，值裡面的 a#b 保留。
            value = re.split(r"[ \t]#", value, maxsplit=1)[0].rstrip()
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


def atomic_write(path, data):
    """同目錄暫存檔 → 權限 600 → os.replace；失敗時刪掉暫存檔，不留含金鑰的檔案。"""
    path = Path(path)
    temporary = None
    try:
        # 暫存檔名有固定前綴，.gitignore 擋得到（萬一程式在 replace 前被殺掉）。
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".tmp-", delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_env_values(values, env_path=None):
    """原子更新 .env 的幾個設定 {名稱: 值或 None}；None＝刪掉那一行。其他行與換行原樣保留，不留備份。"""
    path = Path(ENV_PATH if env_path is None else env_path)
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        text = ""
    names = "|".join(re.escape(name) for name in values)
    pattern = re.compile(rf"([ \t]*(?:export[ \t]+)?({names})[ \t]*=[ \t]*)([^\r\n]*)(\r\n|\r|\n)?")
    lines, changed = [], set()
    for line in text.splitlines(keepends=True):
        found = pattern.fullmatch(line)
        if found:
            # 重複設定也全部換掉，避免最後一行的舊值蓋回來。
            changed.add(found[2])
            value = values[found[2]]
            if value is None:
                continue
            comment = re.search(r"[ \t]+#.*$", found[3])
            line = found[1] + value + (comment[0] if comment else "") + (found[4] or "")
        lines.append(line)
    newline = "\r\n" if "\r\n" in text else "\n"
    for name, value in values.items():
        if name in changed or value is None:
            continue
        if lines and not lines[-1].endswith(("\r", "\n")):
            lines.append(newline)
        lines.append(f"{name}={value}{newline}")
    atomic_write(path, "".join(lines).encode("utf-8"))


def write_api_key(api_key, env_path=None, workspace=False):
    """原子更新金鑰；workspace 給字串或 None 時，連 ANTHROPIC_WORKSPACE_ID 一起寫（None＝刪掉）。
    預設（False）不動 workspace。呼叫端先驗證金鑰。"""
    values = {"ANTHROPIC_API_KEY": api_key}
    if workspace is not False:
        values["ANTHROPIC_WORKSPACE_ID"] = workspace
    write_env_values(values, env_path)


# ----- 多把金鑰：.env 旁邊的 JSON，{"active": 名稱, "keys": {名稱: {"key": 金鑰, "workspace": id 或 null}}} -----
# 舊格式 {名稱: 金鑰} 讀得懂：那時每把金鑰都配 .env 的 ANTHROPIC_WORKSPACE_ID，所以沿用它；下次存檔改成新格式。

KEYS_NAME = ".anthropic_keys.json"
DEFAULT_KEY_NAME = "預設"
MAX_SAVED_KEYS = 10
_KEY_FORMAT = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
_KEY_NAME = re.compile(r"[\w-]{1,20}")      # \w 含中文、字母、數字與 _
_WORKSPACE_FORMAT = re.compile(r"wrkspc_[A-Za-z0-9]{4,80}")


class KeyStoreError(RuntimeError):
    """金鑰清單檔壞了。訊息不含檔案內容。"""


@dataclass
class KeyStore:
    active: str | None
    keys: dict          # 名稱 → 金鑰，依加入順序
    workspaces: dict = field(default_factory=dict)   # 名稱 → workspace id；沒有（綁定 workspace 的金鑰）是 None

    def workspace(self, name):
        return self.workspaces.get(name)


def is_key_format(key):
    return 40 <= len(key) <= 300 and _KEY_FORMAT.fullmatch(key) is not None


def is_workspace_format(workspace):
    """組織層級金鑰要配的 workspace id：wrkspc_ 加字母數字。"""
    return isinstance(workspace, str) and _WORKSPACE_FORMAT.fullmatch(workspace) is not None


def is_key_name(name):
    return _KEY_NAME.fullmatch(name) is not None and "sk-ant-" not in name


def keys_path(env_path=None):
    return Path(ENV_PATH if env_path is None else env_path).with_name(KEYS_NAME)


def _key_entry(value, env_workspace):
    """清單裡的一筆 → (金鑰, workspace)；格式不對回傳 None。"""
    if isinstance(value, str):
        return (value.strip(), env_workspace) if value.strip() else None
    if not isinstance(value, dict):
        return None
    key, workspace = value.get("key"), value.get("workspace")
    if not isinstance(key, str) or not key.strip():
        return None
    if workspace is not None and not (isinstance(workspace, str) and workspace.strip()):
        return None
    return key.strip(), workspace.strip() if workspace else None


def load_key_store(env_path=None):
    """讀金鑰清單；檔案還沒建立時，把 .env 的金鑰（連同 workspace id）當成「預設」（不寫檔）。"""
    path = keys_path(env_path)
    env = read_env_file(ENV_PATH if env_path is None else env_path)
    env_workspace = (env.get("ANTHROPIC_WORKSPACE_ID") or "").strip() or None
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        key = (env.get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            return KeyStore(None, {})
        return KeyStore(DEFAULT_KEY_NAME, {DEFAULT_KEY_NAME: key}, {DEFAULT_KEY_NAME: env_workspace})
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise KeyStoreError("金鑰清單不是有效的 JSON") from None
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, dict) or not all(isinstance(name, str) for name in keys):
        raise KeyStoreError("金鑰清單的格式不對")
    entries = {name: _key_entry(value, env_workspace) for name, value in keys.items()}
    if any(entry is None for entry in entries.values()):
        raise KeyStoreError("金鑰清單的格式不對")
    active = data.get("active")
    return KeyStore(active if isinstance(active, str) and active in keys else None,
                    {name: key for name, (key, _) in entries.items()},
                    {name: workspace for name, (_, workspace) in entries.items()})


def save_key_store(store, env_path=None):
    keys = {name: {"key": key, "workspace": store.workspace(name)} for name, key in store.keys.items()}
    data = {"active": store.active, "keys": keys}
    atomic_write(keys_path(env_path), (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def activate_key_store(store, env_path=None):
    """先寫清單，再把使用中的金鑰與 workspace id 寫進 .env（沒有 workspace 就刪掉那一行）；
    .env 寫不進去就把清單還原，兩邊不會不一致。"""
    path = keys_path(env_path)
    try:
        previous = path.read_bytes()
    except FileNotFoundError:
        previous = None
    save_key_store(store, env_path)
    try:
        write_api_key(store.keys[store.active], env_path, workspace=store.workspace(store.active))
    except BaseException:
        try:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, previous)
        except OSError:
            pass
        raise


def redact(text, *secrets):
    """把訊息裡的金鑰等機密換成 ***（保險用：印出或記錄例外訊息前先過一次）。"""
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    # 輪換後的新金鑰也要遮掉，不依賴啟動時的機密清單。
    return re.sub(r"sk-ant-[A-Za-z0-9_-]+", "***", text)


def client_options(api_key, workspace_id=None):
    """anthropic.Anthropic 的參數：有 workspace id 時用 default_headers 讓每個請求都帶上。"""
    options = {"api_key": api_key}
    if workspace_id:
        options["default_headers"] = {WORKSPACE_HEADER: workspace_id}
    return options


def make_client(api_key, workspace_id=None, *, timeout=None, max_retries=None):
    import anthropic

    options = client_options(api_key, workspace_id)
    if timeout is not None:
        options["timeout"] = timeout
    if max_retries is not None:
        options["max_retries"] = max_retries
    return anthropic.Anthropic(**options)


def default_embedder(timeout=10):
    """查詢向量走 Vertex AI（movie-nas 用預設服務帳號，不需要金鑰）。"""
    from haixia import vertex

    api = vertex.GoogleApi(timeout=timeout, max_attempts=2)
    return vertex.EmbeddingClient(api)


# ---------- 費用 ----------

def default_effort(model):
    """模型的預設 effort：Fable 5.2 是 high，其他（Opus 5.5、Sonnet 5.5…）是 DEFAULT_EFFORT。"""
    return MODEL_EFFORTS.get(model, DEFAULT_EFFORT)


def load_prices(path, base=PRICES):
    """讀 JSON 價格表 {模型: {input, output, cache_read, cache_write}}，覆蓋預設值。"""
    prices = {model: dict(values) for model, values in base.items()}
    for model, values in json.loads(Path(path).read_text(encoding="utf-8")).items():
        missing = [name for name in PRICE_FIELDS if name not in values]
        if missing:
            raise ValueError(f"{model} 的價格缺少 {'、'.join(missing)}")
        prices[model] = {name: float(values[name]) for name in PRICE_FIELDS}
    return prices


def price_for(model, prices=PRICES, today=None, changes=None):
    """某天的價格。prices 是預設價格表時才套用 PRICE_CHANGES；自訂價格表（--prices）原樣使用。"""
    price = prices[model]
    if changes is None:
        changes = PRICE_CHANGES if prices is PRICES else {}
    today = today or datetime.now(timezone.utc).date()
    for start, later in changes.get(model, ()):
        if today >= start:
            price = later
    return price


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


def run_search(searcher, args, remember=None):
    """→ (給 Claude 的文字, 命中清單, 搜尋模式)。"""
    result = searcher.search(args["query"], k=args["k"], kind=KINDS[args["kind"]])
    records = result["results"]
    header = f"查詢「{args['query']}」（範圍：{SCOPE_NAMES[args['kind']]}）"
    if result["mode"] != "hybrid":
        header += "［向量搜尋失敗，這次只用關鍵字搜尋］"
    if not records:
        return header + "：找不到相關段落。可以換關鍵字、同義詞或條文原句再查。", [], result["mode"]
    if remember:
        remember(records)
    blocks = [f"{header}，共 {len(records)} 段："]
    blocks += [format_record(record, f"[{number}]") for number, record in enumerate(records, 1)]
    hits = [{"id": record["id"], "citation": citation(record)} for record in records]
    return "\n\n".join(blocks), hits, result["mode"]


def _resolve_chunk_id(store, chunk_id):
    """出處編號須唯一；其他完整 id 沿用原本的查詢方式。"""
    found = re.fullmatch(r"(?:/s_|s_)?([0-9a-f]{4,})", chunk_id, re.I)
    if found is None:
        if chunk_id.lower().startswith(("/s_", "s_")):
            raise ToolInputError("出處編號要是至少 4 碼的十六進位字元，可帶 /s_ 或 s_ 開頭")
        return chunk_id
    prefix = found.group(1).lower()
    matches = store.find_prefix(prefix, limit=2)
    if not matches:
        raise ToolInputError(f"找不到出處編號：{chunk_id}。索引更新過的話，舊編號會失效。")
    if len(matches) > 1:
        raise ToolInputError(f"出處編號 {chunk_id} 對應到不只一段，請多給幾碼。")
    return matches[0]["id"]


def run_read_context(store, args, remember=None):
    chunk_id = _resolve_chunk_id(store, args["id"])
    try:
        target, previous, following = store.neighbors(chunk_id, args["before"], args["after"])
    except KeyError:
        raise ToolInputError(f"找不到段落 id：{args['id']}") from None
    if remember:
        remember([*previous, target, *following])
    lines = [f"段落 {target['id']}（{citation(target)}）的前後文，依原本順序排列："]
    if not previous and not following:
        lines.append("同一來源裡沒有相鄰的段落（這一段就是開頭或結尾）。")
    for offset, record in enumerate(previous, -len(previous)):
        lines.append(format_record(record, f"[前 {-offset} 段]"))
    # 直接用出處編號查詢時，這段未必出現在搜尋結果，必須一起提供原文。
    cited = chunk_id != args["id"]
    lines.append(format_record(target, "[命中段落]") if cited
                 else f"[命中段落 id={target['id']}：內文見先前的搜尋結果]")
    for offset, record in enumerate(following, 1):
        lines.append(format_record(record, f"[後 {offset} 段]"))
    records = [*previous, *([target] if cited else []), *following]
    hits = [{"id": record["id"], "citation": citation(record)} for record in records]
    return "\n\n".join(lines), hits


def run_classic_commentary(store, args, max_chars=9000, remember=None):
    chunk_id = _resolve_chunk_id(store, args["id"])
    try:
        records = store.classic_commentary(chunk_id)
    except KeyError:
        raise ToolInputError(f"找不到經典段落 id：{args['id']}") from None
    if not records:
        return "這段經典沒有可確認的倪師講義或逐字稿連結。", []
    blocks, hits, used = [], [], 0
    for record in records:
        number = f"（講義第{record['lecture_number']}條）" if record.get("lecture_number") else ""
        marker = f"\n出處：{citation(record)}\n"
        block = f"id={record['id']}｜{KIND_NAMES.get(record['kind'], record['kind'])}{number}{marker}{record['text'].strip()}"
        separator = 2 if blocks else 0
        available = max_chars - used - separator
        if available <= 0:
            break
        if len(block) > available:
            block = block[:max(0, available - 1)] + "…"
        blocks.append(block)
        if remember and marker in block:
            remember([{**record, "text": block.split(marker, 1)[1].removesuffix("…")}])
        hits.append({"id": record["id"], "citation": citation(record)})
        used += len(block) + separator
    return "\n\n".join(blocks), hits


def execute_tool(searcher, name, data, round_number, calls, on_tool=None, conversation=None):
    """兩種模型共用的工具驗證、執行與進度紀錄；回傳 (文字, 是否錯誤)。"""
    record = {"round": round_number, "name": name, "input": data, "hits": [],
              "mode": None, "error": None}
    try:
        args = validate_input(name, data)
        record["input"] = args
        remember = conversation.remember if conversation is not None else None
        if name == "search":
            result, record["hits"], record["mode"] = run_search(searcher, args, remember)
        elif name == "classic_commentary":
            result, record["hits"] = run_classic_commentary(searcher.store, args, remember=remember)
        else:
            result, record["hits"] = run_read_context(searcher.store, args, remember)
    except Exception as problem:  # noqa: BLE001 — 工具失敗要回傳模型
        result = (str(problem) if isinstance(problem, ToolInputError)
                  else f"工具執行失敗（{type(problem).__name__}）：{problem}")
        if not isinstance(data, dict):
            result += "；收到的輸入：" + json.dumps(data, ensure_ascii=False, default=str)[:300]
        record["error"] = result
    calls.append(record)
    if on_tool:
        on_tool(record)
    return result, record["error"] is not None


# ---------- 對話與回答 ----------

class Conversation:
    """多輪對話的歷史。只附加、不改寫：Answerer 只會在 messages 後面加東西。"""

    def __init__(self):
        self.messages = []
        self.questions = 0
        # 出處不含編號 → {段落 id: 原文}；只存工具已回傳的段落。
        self.citations = {}
        self.codes = {}

    def remember(self, records):
        for record in records:
            full = citation(record)
            base, marker, _code = full.rpartition("（編號 ")
            if marker:
                self.citations.setdefault(base, {})[record["id"]] = record["text"]
                self.codes[record["id"]] = _code.removesuffix("）")


_CITATION_END = frozenset("；）) \t\r\n")
_QUOTED_BEFORE = re.compile(r"「([^「」]+)」\s*(?:（出處：|出處：)?$")


def display_answer(text, conversation):
    """只補顯示文字中可確認的編號；不修改模型答案或對話歷史。"""
    if not conversation.citations:
        return text
    bases = sorted(conversation.citations, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(base) for base in bases))

    def replace(match):
        end = match.end()
        if end < len(text) and text[end] not in _CITATION_END:
            return match.group()
        if text.startswith("（編號 ", end):
            return match.group()
        candidates = conversation.citations[match.group()]
        if len(candidates) == 1:
            chunk_id = next(iter(candidates))
        else:
            quoted = _QUOTED_BEFORE.search(text[:match.start()])
            if quoted is None:
                return match.group()
            matched = [chunk_id for chunk_id, body in candidates.items() if quoted.group(1) in body]
            if len(matched) != 1:
                return match.group()
            chunk_id = matched[0]
        # 編號已在工具回傳時算好，不以固定六碼猜測。
        return match.group() + f"（編號 {conversation.codes[chunk_id]}）"

    return pattern.sub(replace, text)


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
    thinking: str = ""


def log_answer(log_dir, model, effort, fallback_enabled, question, answer, error):
    """兩個 provider 共用 JSONL 欄位；只記錯誤類型與狀態碼。"""
    if log_dir is None:
        return
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model, "served_model": answer.model, "effort": effort,
        "fallback_enabled": fallback_enabled, "fallback_ran": answer.fallback,
        "question": question, "answer": answer.text, "thinking": answer.thinking,
        "stop_reason": answer.stop_reason,
        "refused": answer.refused, "rounds": answer.rounds, "requests": answer.requests,
        "tool_calls": answer.tool_calls, "usage": answer.usage,
        "cost_usd": round(answer.cost_usd, 6), "elapsed_sec": answer.elapsed_sec,
        "notes": answer.notes,
        "error": None if error is None else {
            "type": type(error).__name__,
            "status": getattr(error, "status_code", None) or getattr(error, "code", None)},
    }
    try:
        path = Path(log_dir)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(path / LOG_NAME, "a", encoding="utf-8") as log:
            log.write(redact(json.dumps(entry, ensure_ascii=False, default=str)) + "\n")
    except OSError as problem:
        answer.notes.append(f"寫入 log 失敗：{type(problem).__name__}")


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
    def __init__(self, index_dir, model=DEFAULT_MODEL, effort=None, *, client=None,
                 api_key=None, workspace_id=None, embedder="vertex", system_prompt=None,
                 max_tool_rounds=MAX_TOOL_ROUNDS, max_tokens=MAX_TOKENS, fallback=None,
                 prefix_mismatch="drop_block",
                 log_dir=DEFAULT_LOG_DIR, prices=None, clock=time.monotonic, searcher=None):
        """client：anthropic.Anthropic（或同介面物件）；None 時用 api_key（與 workspace_id）建立。

        effort：None＝依模型的預設（default_effort）。
        embedder："vertex"＝用 Vertex 算查詢向量；None＝只用 BM25；或傳入同介面物件（測試用）。
        fallback：None＝依模型決定（Opus 5.5 等預設開）；True／False 強制。
        prefix_mismatch：思考區塊綁定檢查不符時的處理（"drop_block"／"error"／None＝不送）。
        log_dir：None＝不寫 JSONL。
        searcher：共用現成的 Searcher（Telegram bot 切換模型時用），這時不看 index_dir 與 embedder；
        close() 也不會關掉它，由建立的人負責。
        """
        if effort is None:
            effort = default_effort(model)
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
        self.owns_client = client is None
        self.client = client if client is not None else make_client(api_key, workspace_id)

    # ----- 請求 -----

    def request_params(self, messages, allow_tools=True):
        thinking = {"type": "adaptive", "display": "summarized"}
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

    def _run_tool(self, block, round_number, calls, on_tool, conversation):
        message, failed = execute_tool(self.searcher, block.name, block.input, round_number, calls,
                                       on_tool, conversation)
        result = {"type": "tool_result", "tool_use_id": block.id, "content": message}
        if failed:
            result["is_error"] = True
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
            self._loop(messages, answer, on_tool, conversation)
        except Exception as problem:
            error = problem
            raise
        finally:
            answer.elapsed_sec = round(self.clock() - started, 3)
            self._log(question, answer, error)
        return answer

    def _loop(self, messages, answer, on_tool, conversation):
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

            summaries = [block.thinking for block in response.content
                         if block.type == "thinking" and getattr(block, "thinking", "")]
            if summaries:
                answer.thinking = "\n\n".join(filter(None, [answer.thinking, *summaries]))

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
                results = [self._run_tool(block, answer.rounds, answer.tool_calls, on_tool, conversation)
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
        log_answer(self.log_dir, self.model, self.effort, self.fallback, question, answer, error)

    def close(self):
        try:
            if self.owns_client:
                self.client.close()
        finally:
            if self.owns_searcher:
                self.searcher.close()
