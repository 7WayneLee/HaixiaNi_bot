"""Vertex 向量與 Vision 文字辨識客戶端；全部用假的 transport，不連網。"""

import json
import threading

import numpy as np
import pytest

from haixia import vertex


class FakeTransport:
    """依 URL 回應；記錄每次呼叫。handler(method, url, headers, payload) → (status, 物件或 bytes)。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body) if body else None
        with self.lock:
            self.calls.append((method, url, headers, payload))
        status, data = self.handler(method, url, headers, payload)
        return status, data if isinstance(data, bytes) else json.dumps(data).encode()


class StaticToken:
    def __init__(self):
        self.invalidated = 0

    def get(self):
        return "tok"

    def invalidate(self):
        self.invalidated += 1


def fake_vector(text, dims=768):
    """依文字產生可重現的向量（不是單位長度，用來檢查正規化）。"""
    rng = np.random.default_rng(abs(hash(text)) % (2 ** 32))
    return (rng.standard_normal(dims) * 3).tolist()


def embed_handler(fail=None, counter=None):
    def handler(method, url, headers, payload):
        if counter is not None:
            counter.append(len(payload["instances"]))
        if fail:
            result = fail(payload)
            if result:
                return result
        predictions = [{"embeddings": {"values": fake_vector(i["content"]),
                                       "statistics": {"token_count": len(i["content"]), "truncated": False}}}
                       for i in payload["instances"]]
        return 200, {"predictions": predictions}
    return handler


def make_api(handler, sleeps=None):
    transport = FakeTransport(handler)
    api = vertex.GoogleApi(token=StaticToken(), transport=transport, max_attempts=5,
                           sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return api, transport


def test_metadata_token_is_cached_and_refreshed():
    now = [1000.0]
    responses = iter([(200, {"access_token": "a", "expires_in": 120}), (200, {"access_token": "b", "expires_in": 3600})])
    transport = FakeTransport(lambda m, u, h, p: next(responses))
    token = vertex.MetadataToken(transport=transport, clock=lambda: now[0])
    assert token.get() == "a" and token.get() == "a"
    assert len(transport.calls) == 1
    method, url, headers, _ = transport.calls[0]
    assert url == vertex.METADATA_TOKEN_URL and headers == {"Metadata-Flavor": "Google"}
    now[0] += 61                              # 到期前 60 秒內要更新
    assert token.get() == "b"
    token.invalidate()
    with pytest.raises(StopIteration):
        token.get()


def test_embed_request_format_and_parsing():
    api, transport = make_api(embed_handler())
    client = vertex.EmbeddingClient(api, project="p1", location="us-central1")
    result = client.embed(["桂枝湯", "麻黃湯"], "RETRIEVAL_DOCUMENT", titles=["人紀・傷寒論", None])
    method, url, headers, payload = transport.calls[0]
    assert method == "POST"
    assert url == ("https://us-central1-aiplatform.googleapis.com/v1/projects/p1/locations/us-central1/"
                   "publishers/google/models/gemini-embedding-001:predict")
    assert headers["Authorization"] == "Bearer tok"
    assert payload == {"instances": [{"content": "桂枝湯", "task_type": "RETRIEVAL_DOCUMENT", "title": "人紀・傷寒論"},
                                     {"content": "麻黃湯", "task_type": "RETRIEVAL_DOCUMENT"}],
                       "parameters": {"outputDimensionality": 768}}
    assert len(result) == 2 and len(result[0][0]) == 768 and result[0][1] == 3 and result[0][2] is False
    client.embed(["問題"], "RETRIEVAL_QUERY", titles=["不該送出"])
    assert "title" not in transport.calls[1][3]["instances"][0]


def test_embed_rejects_oversized_batches_and_bad_responses():
    api, _ = make_api(lambda m, u, h, p: (200, {"predictions": []}))
    client = vertex.EmbeddingClient(api)
    with pytest.raises(ValueError):
        client.embed(["a"] * 251, "RETRIEVAL_DOCUMENT")
    with pytest.raises(ValueError):
        client.embed(["字" * 15000, "字" * 6000], "RETRIEVAL_DOCUMENT")
    with pytest.raises(vertex.ApiError, match="筆數"):
        client.embed(["a"], "RETRIEVAL_DOCUMENT")


def test_retry_on_429_and_5xx_with_exponential_backoff():
    responses = iter([(429, b"quota"), (503, b"busy"), None])

    def handler(method, url, headers, payload):
        response = next(responses)
        return response or embed_handler()(method, url, headers, payload)

    sleeps = []
    api, transport = make_api(handler, sleeps)
    vertex.EmbeddingClient(api).embed(["桂枝"], "RETRIEVAL_QUERY")
    assert len(transport.calls) == 3
    assert len(sleeps) == 2 and 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0


def test_no_retry_on_400_and_401_refreshes_token():
    api, transport = make_api(lambda m, u, h, p: (400, b"bad request"))
    with pytest.raises(vertex.ApiError, match="400"):
        vertex.EmbeddingClient(api).embed(["a"], "RETRIEVAL_QUERY")
    assert len(transport.calls) == 1

    responses = iter([(401, b"expired"), None])
    api, transport = make_api(lambda m, u, h, p: next(responses) or embed_handler()(m, u, h, p))
    vertex.EmbeddingClient(api).embed(["a"], "RETRIEVAL_QUERY")
    assert api.token.invalidated == 1 and len(transport.calls) == 2


def test_retries_give_up_after_max_attempts():
    api, transport = make_api(lambda m, u, h, p: (500, b"err"))
    with pytest.raises(vertex.RetryableError):
        vertex.EmbeddingClient(api).embed(["a"], "RETRIEVAL_QUERY")
    assert len(transport.calls) == 5


def write_chunks(path, count):
    with open(path, "w", encoding="utf-8") as output:
        for i in range(count):
            output.write(json.dumps({"id": f"c{i}", "kind": "document", "title": "書", "episode": None,
                                     "section": f"第{i}節", "text": f"第{i}段內容" * 5}, ensure_ascii=False) + "\n")


def test_embed_corpus_batches_writes_normalized_rows_in_order(tmp_path):
    write_chunks(tmp_path / "chunks.jsonl", 7)
    sizes = []
    api, transport = make_api(embed_handler(counter=sizes))
    client = vertex.EmbeddingClient(api)
    cache = vertex.EmbedCache(tmp_path / "cache.sqlite")
    meta = vertex.embed_corpus(tmp_path / "chunks.jsonl", tmp_path, client, cache, batch_size=3, jobs=2,
                               log=lambda m: None)
    assert sorted(sizes) == [1, 3, 3]
    matrix = np.load(tmp_path / "embeddings.f16.npy", mmap_mode="r")
    assert matrix.shape == (7, 768) and matrix.dtype == np.float16
    assert np.allclose(np.linalg.norm(matrix.astype(np.float32), axis=1), 1.0, atol=2e-3)
    expected = np.asarray(fake_vector("第4段內容" * 5), dtype=np.float32)
    assert np.allclose(matrix[4].astype(np.float32), expected / np.linalg.norm(expected), atol=2e-3)
    saved = json.loads((tmp_path / "embeddings.meta.json").read_text(encoding="utf-8"))
    assert saved == meta
    assert meta["count"] == 7 and meta["dims"] == 768 and meta["model"] == "gemini-embedding-001"
    assert meta["tokens"] == 7 * len("第0段內容" * 5)
    assert meta["estimated_cost_usd"] == round(meta["tokens"] / 1e6 * 0.15, 4)
    # 送出時帶標題（課名＋章節）
    titles = {i["title"] for _, _, _, payload in transport.calls for i in payload["instances"]}
    assert "書 第0節" in titles


def test_embed_corpus_resumes_from_cache_after_failure(tmp_path):
    write_chunks(tmp_path / "chunks.jsonl", 6)
    sent = []

    def fail_on_third(payload):
        if len(sent) >= 3:
            return 400, b"stop"
        sent.append(payload["instances"][0]["content"])
        return None

    api, _ = make_api(embed_handler(fail=fail_on_third))
    cache = vertex.EmbedCache(tmp_path / "cache.sqlite")
    with pytest.raises(vertex.ApiError):
        vertex.embed_corpus(tmp_path / "chunks.jsonl", tmp_path, vertex.EmbeddingClient(api), cache,
                            jobs=1, log=lambda m: None)
    assert not (tmp_path / "embeddings.f16.npy").exists()
    cache.close()

    counter = []
    api, _ = make_api(embed_handler(counter=counter))
    cache = vertex.EmbedCache(tmp_path / "cache.sqlite")
    meta = vertex.embed_corpus(tmp_path / "chunks.jsonl", tmp_path, vertex.EmbeddingClient(api), cache,
                               jobs=1, log=lambda m: None)
    assert sum(counter) == 3                     # 只送沒做完的三段
    assert meta["count"] == 6
    counter.clear()
    vertex.embed_corpus(tmp_path / "chunks.jsonl", tmp_path, vertex.EmbeddingClient(api), cache,
                        jobs=1, log=lambda m: None)
    assert counter == []                         # 全部都有快取時完全不呼叫


def test_embed_corpus_stops_at_token_limit_and_explains_how_to_raise(tmp_path):
    write_chunks(tmp_path / "chunks.jsonl", 5)
    counter = []
    api, _ = make_api(embed_handler(counter=counter))
    cache = vertex.EmbedCache(tmp_path / "cache.sqlite")
    messages = []
    with pytest.raises(vertex.TokenLimitReached, match="--max-tokens"):
        vertex.embed_corpus(tmp_path / "chunks.jsonl", tmp_path, vertex.EmbeddingClient(api), cache,
                            jobs=1, max_tokens=100, log=messages.append)
    assert 0 < sum(counter) < 5
    assert not (tmp_path / "embeddings.f16.npy").exists()
    meta = vertex.embed_corpus(tmp_path / "chunks.jsonl", tmp_path, vertex.EmbeddingClient(api), cache,
                               jobs=1, max_tokens=10_000, log=messages.append)
    assert sum(counter) == 5 and meta["count"] == 5
    assert any("累計" in message for message in messages)


def test_cache_key_depends_on_model_dims_task_and_text():
    base = vertex.cache_key("m", 768, "RETRIEVAL_DOCUMENT", "t", "x")
    assert base != vertex.cache_key("m", 3072, "RETRIEVAL_DOCUMENT", "t", "x")
    assert base != vertex.cache_key("m", 768, "RETRIEVAL_QUERY", "t", "x")
    assert base != vertex.cache_key("m", 768, "RETRIEVAL_DOCUMENT", "u", "x")
    assert base == vertex.cache_key("m", 768, "RETRIEVAL_DOCUMENT", "t", "x")


# ---------- Vision ----------

def test_vision_submit_poll_and_parse():
    polls = iter([{"name": "op", "metadata": {"state": "RUNNING"}},
                  {"name": "op", "done": True, "response": {}}])

    def handler(method, url, headers, payload):
        if url.endswith("files:asyncBatchAnnotate"):
            return 200, {"name": "projects/p/operations/op"}
        if "/operations/" in url:
            return 200, next(polls)
        raise AssertionError(url)

    sleeps = []
    api, transport = make_api(handler)
    ocr = vertex.VisionOCR(api, sleep=sleeps.append, log=lambda m: None)
    name = ocr.submit("gs://b/raw/天機道.pdf", "gs://b/ocr/tianjidao/")
    assert name == "projects/p/operations/op"
    request = transport.calls[0][3]["requests"][0]
    assert request["inputConfig"] == {"gcsSource": {"uri": "gs://b/raw/天機道.pdf"}, "mimeType": "application/pdf"}
    assert request["features"] == [{"type": "DOCUMENT_TEXT_DETECTION"}]
    assert request["imageContext"] == {"languageHints": ["zh"]}
    assert request["outputConfig"]["gcsDestination"] == {"uri": "gs://b/ocr/tianjidao/"}
    ocr.wait(name, poll_sec=7)
    assert sleeps == [7]
    assert transport.calls[-1][1] == "https://vision.googleapis.com/v1/projects/p/operations/op"


def test_vision_wait_reports_errors_and_timeouts():
    api, _ = make_api(lambda m, u, h, p: (200, {"done": True, "error": {"message": "壞掉"}}))
    with pytest.raises(vertex.ApiError, match="壞掉"):
        vertex.VisionOCR(api, log=lambda m: None).wait("op")
    now = [0.0]
    api, _ = make_api(lambda m, u, h, p: (200, {"done": False}))
    ocr = vertex.VisionOCR(api, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0],
                           log=lambda m: None)
    with pytest.raises(vertex.ApiError, match="仍未完成"):
        ocr.wait("op", poll_sec=10, timeout_sec=30)


def test_vision_lists_and_downloads_outputs_with_pagination():
    pages = {None: {"items": [{"name": "ocr/t/output-1-to-2.json"}, {"name": "ocr/t/readme.txt"}],
                    "nextPageToken": "n2"},
             "n2": {"items": [{"name": "ocr/t/output-3-to-3.json"}]}}

    def handler(method, url, headers, payload):
        if "alt=media" in url:
            assert "ocr%2Ft%2Foutput-1-to-2.json" in url
            return 200, {"responses": []}
        token = "n2" if "pageToken=n2" in url else None
        assert "prefix=ocr%2Ft%2F" in url
        return 200, pages[token]

    api, _ = make_api(handler)
    ocr = vertex.VisionOCR(api, log=lambda m: None)
    assert ocr.list_outputs("gs://b/ocr/t/") == ["ocr/t/output-1-to-2.json", "ocr/t/output-3-to-3.json"]
    assert ocr.download_json("b", "ocr/t/output-1-to-2.json") == {"responses": []}


def test_parse_vision_outputs_orders_pages_and_rejects_errors():
    documents = [
        {"responses": [{"fullTextAnnotation": {"text": "第三頁"}, "context": {"pageNumber": 3}}]},
        {"responses": [{"fullTextAnnotation": {"text": "第一頁"}, "context": {"pageNumber": 1}},
                       {"context": {"pageNumber": 2}}]},
    ]
    assert vertex.parse_vision_outputs(documents) == [
        {"page": 1, "text": "第一頁"}, {"page": 2, "text": ""}, {"page": 3, "text": "第三頁"}]
    with pytest.raises(vertex.ApiError, match="第 5 頁"):
        vertex.parse_vision_outputs([{"responses": [{"error": {"message": "x"}, "context": {"pageNumber": 5}}]}])


def test_cmd_ocr_submits_once_then_resumes(tmp_path, monkeypatch):
    from scripts import build_index

    state = {"listed": 0, "submitted": 0}

    def handler(method, url, headers, payload):
        if url.endswith("files:asyncBatchAnnotate"):
            state["submitted"] += 1
            return 200, {"name": "projects/p/operations/op"}
        if "/operations/" in url:
            return 200, {"done": True}
        if "alt=media" in url:
            return 200, {"responses": [{"fullTextAnnotation": {"text": f"頁{n}"}, "context": {"pageNumber": n}}
                                       for n in (1, 2)]}
        state["listed"] += 1
        items = [{"name": "ocr/tianjidao/output-1-to-2.json"}] if state["submitted"] else []
        return 200, {"items": items}

    transport = FakeTransport(handler)
    real = vertex.GoogleApi
    monkeypatch.setattr(build_index.vertex, "GoogleApi",
                        lambda **kw: real(token=StaticToken(), transport=transport, sleep=lambda s: None))
    args = ["ocr", "--out-dir", str(tmp_path), "--bucket", "b", "--expect-pages", "2", "--poll-sec", "1"]
    assert build_index.main(args) == 0
    pages = json.loads((tmp_path / "ocr/tianjidao.pages.json").read_text(encoding="utf-8"))
    assert pages["source"] == build_index.TIANJIDAO
    assert [p["page"] for p in pages["pages"]] == [1, 2]
    assert state["submitted"] == 1
    assert build_index.main(args) == 0          # 已有結果：不再送出
    assert state["submitted"] == 1
