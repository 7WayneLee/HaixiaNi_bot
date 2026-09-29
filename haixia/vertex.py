"""Vertex AI 向量與 Cloud Vision 文字辨識的精簡客戶端（只用標準函式庫）。

認證：向 GCE metadata server 取預設服務帳號的 access token，不用金鑰。
所有網路呼叫都經過可替換的 transport(method, url, headers, body, timeout)
→ (status, bytes)，測試用假的 transport，不會打真的 API。
"""

import hashlib
import json
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PROJECT = "vmdemo1-507014"
DEFAULT_LOCATION = "us-central1"
DEFAULT_BUCKET = "haixiani-bot-data-507014"
MODEL = "gemini-embedding-001"
DIMS = 768
# 官方文件（Vertex AI「Get text embeddings」→ API limits）：每次請求最多 250 筆、
# 合計 20,000 token，超過回 400；單筆超過 2,048 token 會被截斷。
# 同一頁也寫「gemini-embedding-001 每次請求只能有一筆」，所以預設一次一筆；
# 指揮實測多筆可用，要加大時用 --batch-size，但仍受下面兩個上限限制。
MAX_INSTANCES = 250
MAX_REQUEST_TOKENS = 20_000
MAX_INPUT_TOKENS = 2_048
PRICE_PER_MTOK = 0.15   # 美元／百萬 token（gemini-embedding-001，Vertex AI 線上價格）
METADATA_TOKEN_URL = ("http://metadata.google.internal/computeMetadata/v1/"
                      "instance/service-accounts/default/token")
RETRYABLE = {408, 429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class RetryableError(ApiError):
    pass


class TokenLimitReached(RuntimeError):
    pass


def urllib_transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class MetadataToken:
    """快取 access token，到期前 60 秒更新。"""

    def __init__(self, transport=urllib_transport, clock=time.time, timeout=10):
        self.transport = transport
        self.clock = clock
        self.timeout = timeout
        self._token = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self._token is None or self.clock() > self._expires - 60:
                try:
                    status, body = self.transport("GET", METADATA_TOKEN_URL,
                                                  {"Metadata-Flavor": "Google"}, None, self.timeout)
                except OSError as error:
                    raise RetryableError(f"無法連到 metadata server：{error}") from None
                if status != 200:
                    raise ApiError(f"metadata server 回應 {status}", status)
                data = json.loads(body)
                self._token = data["access_token"]
                self._expires = self.clock() + float(data.get("expires_in", 300))
            return self._token

    def invalidate(self):
        with self._lock:
            self._token = None


def with_retry(call, max_attempts=6, base_delay=1.0, max_delay=60.0, sleep=time.sleep, log=None):
    """429、5xx、逾時與連線錯誤用指數退避加隨機抖動重試。"""
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except RetryableError as error:
            if attempt == max_attempts:
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1)) * (0.5 + random.random() / 2)
            if log:
                log(f"暫時失敗（{error}），{delay:.1f} 秒後重試（第 {attempt}/{max_attempts - 1} 次）")
            sleep(delay)


class GoogleApi:
    """帶認證與重試的 JSON 呼叫。"""

    def __init__(self, token=None, transport=urllib_transport, timeout=60, max_attempts=6,
                 sleep=time.sleep, log=None):
        self.transport = transport
        self.token = token or MetadataToken(transport)
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.log = log

    def _once(self, method, url, payload, raw):
        headers = {"Authorization": f"Bearer {self.token.get()}"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            status, data = self.transport(method, url, headers, body, self.timeout)
        except (OSError, TimeoutError) as error:
            raise RetryableError(f"連線錯誤：{error}") from None
        if status == 401:
            self.token.invalidate()
            raise RetryableError("認證過期（401）", status)
        if status in RETRYABLE:
            raise RetryableError(f"HTTP {status}", status)
        if status >= 400:
            message = data[:300].decode("utf-8", errors="replace")
            raise ApiError(f"HTTP {status}：{message}", status)
        return data if raw else json.loads(data or b"{}")

    def call(self, method, url, payload=None, raw=False):
        return with_retry(lambda: self._once(method, url, payload, raw), self.max_attempts,
                          sleep=self.sleep, log=self.log)


# ---------- 向量 ----------

def estimate_tokens(text):
    """保守估計：中日韓字每字 1 token，其他字元每 3 字 1 token。"""
    cjk = sum(1 for char in text if ord(char) >= 0x2E80)
    return cjk + (len(text) - cjk + 2) // 3 + 1


class EmbeddingClient:
    def __init__(self, api, project=DEFAULT_PROJECT, location=DEFAULT_LOCATION,
                 model=MODEL, dims=DIMS):
        self.api = api
        self.model = model
        self.dims = dims
        self.url = (f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}/"
                    f"locations/{location}/publishers/google/models/{model}:predict")

    def embed(self, texts, task_type, titles=None):
        """回傳 [(向量 list, token 數, 是否截斷)]，順序同 texts。"""
        if not texts:
            return []
        if len(texts) > MAX_INSTANCES:
            raise ValueError(f"一次最多 {MAX_INSTANCES} 筆")
        if sum(estimate_tokens(t) for t in texts) > MAX_REQUEST_TOKENS:
            raise ValueError(f"一次請求估計超過 {MAX_REQUEST_TOKENS} token")
        instances = []
        for index, text in enumerate(texts):
            instance = {"content": text, "task_type": task_type}
            if titles and titles[index] and task_type == "RETRIEVAL_DOCUMENT":
                instance["title"] = titles[index]
            instances.append(instance)
        payload = {"instances": instances, "parameters": {"outputDimensionality": self.dims}}
        data = self.api.call("POST", self.url, payload)
        predictions = data.get("predictions") or []
        if len(predictions) != len(texts):
            raise ApiError(f"回傳筆數 {len(predictions)} 與送出筆數 {len(texts)} 不符")
        result = []
        for prediction in predictions:
            embedding = prediction["embeddings"]
            values = embedding["values"]
            if len(values) != self.dims:
                raise ApiError(f"向量維度 {len(values)} 不是 {self.dims}")
            statistics = embedding.get("statistics", {})
            result.append((values, int(statistics.get("token_count", 0)),
                           bool(statistics.get("truncated", False))))
        return result


def normalize(values):
    import numpy as np

    vector = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        raise ValueError("向量長度為零")
    return vector / norm


def embed_title(chunk):
    return " ".join(part for part in (chunk.get("title"), chunk.get("episode"), chunk.get("section")) if part)


def cache_key(model, dims, task_type, title, text):
    digest = hashlib.sha1(f"{title or ''}\x1f{text}".encode("utf-8")).hexdigest()
    return f"{model}|{dims}|{task_type}|{digest}"


class EmbedCache:
    """SQLite 快取：key → L2 正規化後的 float16 向量與 token 數。"""

    def __init__(self, path):
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, "
                                "vec BLOB NOT NULL, tokens INTEGER NOT NULL, truncated INTEGER NOT NULL)")
        self.connection.commit()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            row = self.connection.execute("SELECT vec, tokens, truncated FROM cache WHERE key = ?",
                                          (key,)).fetchone()
        return row

    def put_many(self, rows):
        import numpy as np

        with self.lock:
            self.connection.executemany(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)",
                [(key, normalize(values).astype(np.float16).tobytes(), tokens, int(truncated))
                 for key, values, tokens, truncated in rows])
            self.connection.commit()

    def close(self):
        self.connection.close()


def _read_chunks(path):
    with open(path, encoding="utf-8") as source:
        for index, line in enumerate(source):
            chunk = json.loads(line)
            yield index, chunk


def embed_corpus(chunks_path, out_dir, client, cache, *, task_type="RETRIEVAL_DOCUMENT",
                 batch_size=1, jobs=4, max_tokens=15_000_000, price_per_mtok=PRICE_PER_MTOK,
                 log=print, progress_every=200, clock=time.time):
    """讀 chunks.jsonl，把沒有快取的段落送去算向量，最後依列順序寫出 npy。

    不會一次把全部內文或向量放進記憶體：第一輪只算快取與估計量，
    第二輪邊讀邊送，第三輪從快取依序寫進 memmap。
    累計 token（已快取 + 本次）超過 max_tokens 就停下並拋出 TokenLimitReached。
    """
    import numpy as np

    if not 1 <= batch_size <= MAX_INSTANCES:
        raise ValueError(f"batch_size 必須介於 1 到 {MAX_INSTANCES}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    key_of = lambda chunk: cache_key(client.model, client.dims, task_type, embed_title(chunk), chunk["text"])

    total = cached = cached_tokens = pending_estimate = 0
    for _index, chunk in _read_chunks(chunks_path):
        total += 1
        row = cache.get(key_of(chunk))
        if row:
            cached += 1
            cached_tokens += row[1]
        else:
            pending_estimate += estimate_tokens(embed_title(chunk) + chunk["text"])
    log(f"共 {total} 段；已快取 {cached} 段（{cached_tokens:,} token），"
        f"待送 {total - cached} 段（估計約 {pending_estimate:,} token）")

    spent = cached_tokens
    in_flight = 0
    state = {"sent": 0, "truncated": 0, "new_tokens": 0}
    lock = threading.Lock()

    def run_batch(batch):
        texts = [chunk["text"] for chunk in batch]
        titles = [embed_title(chunk) for chunk in batch]
        results = client.embed(texts, task_type, titles)
        rows = [(key_of(chunk), values, tokens, truncated)
                for chunk, (values, tokens, truncated) in zip(batch, results)]
        cache.put_many(rows)
        return sum(r[2] for r in rows), sum(1 for r in rows if r[3]), len(rows)

    def batches():
        batch, batch_tokens = [], 0
        seen = set()
        for _index, chunk in _read_chunks(chunks_path):
            key = key_of(chunk)
            if key in seen or cache.get(key):
                continue
            seen.add(key)
            estimate = estimate_tokens(embed_title(chunk) + chunk["text"])
            if batch and (len(batch) >= batch_size or batch_tokens + estimate > MAX_REQUEST_TOKENS):
                yield batch, batch_tokens
                batch, batch_tokens = [], 0
            batch.append(chunk)
            batch_tokens += estimate
        if batch:
            yield batch, batch_tokens

    stopped = None
    started = clock()
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {}
        iterator = batches()

        def collect(done):
            nonlocal spent, in_flight
            for future in done:
                estimate = futures.pop(future)
                tokens, truncated, count = future.result()
                with lock:
                    in_flight -= estimate
                    spent += tokens
                    state["new_tokens"] += tokens
                    state["truncated"] += truncated
                    before = state["sent"]
                    state["sent"] += count
                if state["sent"] // progress_every != before // progress_every:
                    log(f"已送 {state['sent']}/{total - cached} 段，累計 {spent:,} token，"
                        f"經過 {clock() - started:.0f} 秒")

        try:
            for batch, estimate in iterator:
                while len(futures) >= jobs:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    collect(done)
                if spent + in_flight + estimate > max_tokens:
                    stopped = (f"累計 token 將超過上限 {max_tokens:,}（目前 {spent:,}）。已完成的段落在快取裡；"
                               f"確認費用後用 --max-tokens 放寬（例如 --max-tokens {max_tokens * 2:,}）"
                               "重跑，會從快取續跑。")
                    break
                in_flight += estimate
                futures[executor.submit(run_batch, batch)] = estimate
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                collect(done)
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    log(f"本次送出 {state['sent']} 段、{state['new_tokens']:,} token；累計 {spent:,} token"
        + (f"；{state['truncated']} 段被截斷" if state["truncated"] else ""))
    if stopped:
        raise TokenLimitReached(stopped)

    # 依列順序寫出；先寫暫存檔，完成後才改名。
    temp = out_dir / "embeddings.f16.npy.tmp"
    matrix = np.lib.format.open_memmap(temp, mode="w+", dtype=np.float16, shape=(total, client.dims))
    digest = hashlib.sha256()
    truncated_total = 0
    for index, chunk in _read_chunks(chunks_path):
        row = cache.get(key_of(chunk))
        if row is None:
            raise RuntimeError(f"第 {index} 段沒有向量，請重跑 embed")
        matrix[index] = np.frombuffer(row[0], dtype=np.float16)
        truncated_total += row[2]
    matrix.flush()
    del matrix
    temp.replace(out_dir / "embeddings.f16.npy")
    with open(chunks_path, "rb") as source:
        while block := source.read(1 << 20):
            digest.update(block)
    meta = {
        "model": client.model, "dims": client.dims, "task_type": task_type, "dtype": "float16",
        "normalized": "l2", "count": total, "tokens": spent, "truncated": truncated_total,
        "estimated_cost_usd": round(spent / 1_000_000 * price_per_mtok, 4),
        "price_per_mtok_usd": price_per_mtok, "chunks_sha256": digest.hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    (out_dir / "embeddings.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n",
                                                  encoding="utf-8")
    return meta


# ---------- Cloud Vision 文字辨識 ----------

VISION_URL = "https://vision.googleapis.com/v1"
STORAGE_URL = "https://storage.googleapis.com/storage/v1"


def split_gcs(uri):
    if not uri.startswith("gs://") or "/" not in uri[5:]:
        raise ValueError(f"不是 gs:// 路徑：{uri}")
    bucket, name = uri[5:].split("/", 1)
    return bucket, name


class VisionOCR:
    def __init__(self, api, sleep=time.sleep, clock=time.time, log=print):
        self.api = api
        self.sleep = sleep
        self.clock = clock
        self.log = log

    def submit(self, pdf_uri, output_prefix, language_hints=("zh",), pages_per_file=20):
        payload = {"requests": [{
            "inputConfig": {"gcsSource": {"uri": pdf_uri}, "mimeType": "application/pdf"},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            "imageContext": {"languageHints": list(language_hints)},
            "outputConfig": {"gcsDestination": {"uri": output_prefix}, "batchSize": pages_per_file},
        }]}
        data = self.api.call("POST", f"{VISION_URL}/files:asyncBatchAnnotate", payload)
        if not data.get("name"):
            raise ApiError("Vision 沒有回傳工作名稱")
        return data["name"]

    def wait(self, operation, poll_sec=15, timeout_sec=3600):
        deadline = self.clock() + timeout_sec
        while True:
            data = self.api.call("GET", f"{VISION_URL}/{operation}")
            if data.get("done"):
                if "error" in data:
                    raise ApiError(f"Vision 工作失敗：{data['error'].get('message', data['error'])}")
                return data
            state = data.get("metadata", {}).get("state", "執行中")
            if self.clock() > deadline:
                raise ApiError(f"Vision 工作超過 {timeout_sec} 秒仍未完成（狀態 {state}）")
            self.log(f"Vision 工作狀態：{state}，{poll_sec} 秒後再查")
            self.sleep(poll_sec)

    def list_outputs(self, prefix_uri):
        bucket, prefix = split_gcs(prefix_uri)
        names, token = [], None
        while True:
            query = {"prefix": prefix, "fields": "items(name),nextPageToken"}
            if token:
                query["pageToken"] = token
            data = self.api.call("GET", f"{STORAGE_URL}/b/{bucket}/o?{urllib.parse.urlencode(query)}")
            names += [item["name"] for item in data.get("items", []) if item["name"].endswith(".json")]
            token = data.get("nextPageToken")
            if not token:
                return sorted(names)

    def download_json(self, bucket, name):
        quoted = urllib.parse.quote(name, safe="")
        return json.loads(self.api.call("GET", f"{STORAGE_URL}/b/{bucket}/o/{quoted}?alt=media", raw=True))


def parse_vision_outputs(documents):
    """把 Vision 的輸出 JSON 轉成 [{"page", "text"}]，依頁碼排序。"""
    pages = {}
    for document in documents:
        for response in document.get("responses", []):
            if "error" in response:
                raise ApiError(f"Vision 第 {response.get('context', {}).get('pageNumber')} 頁失敗："
                               f"{response['error'].get('message')}")
            number = response.get("context", {}).get("pageNumber")
            if number is None:
                raise ApiError("Vision 輸出缺少頁碼")
            pages[int(number)] = response.get("fullTextAnnotation", {}).get("text", "")
    return [{"page": number, "text": pages[number]} for number in sorted(pages)]
