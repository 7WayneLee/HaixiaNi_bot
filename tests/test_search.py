"""FTS5 雙字 BM25、memmap 向量、RRF 合併與退回 BM25；向量查詢用假的 embedder。"""

import json

import numpy as np
import pytest

from haixia.index_store import IndexStore, build_db, index_tokens, match_query
from haixia.search import Searcher, citation, rrf_merge

CHUNKS = [
    {"id": "a", "kind": "transcript", "source": "影片/05 傷寒論/伤寒论1（1）.rmvb", "title": "人紀・傷寒論",
     "episode": "傷寒論1（1）", "section": None, "page_start": None, "page_end": None, "start": 65.2,
     "end": 190.9, "date": None, "text": "桂枝湯是五味藥，桂枝、芍藥、甘草、生薑、大棗。"},
    {"id": "b", "kind": "document", "source": "文字資料/人紀.pdf", "title": "人紀《傷寒論》", "episode": None,
     "section": "辨太陽病", "page_start": 12, "page_end": 13, "start": None, "end": None, "date": None,
     "text": "麻黃湯治太陽傷寒，無汗而喘。"},
    {"id": "c", "kind": "document", "source": "文字資料/事實評論一.doc", "title": "事實評論一", "episode": None,
     "section": None, "page_start": None, "page_end": None, "start": None, "end": None, "date": "2008-08-01",
     "text": "病人出汗很多，這是表虛。"},
    {"id": "d", "kind": "transcript", "source": "影片/02 針灸/针灸1（1）.rmvb", "title": "人紀・針灸",
     "episode": "針灸1（1）", "section": None, "page_start": None, "page_end": None, "start": 3600.0,
     "end": 3725.0, "date": None, "text": "太衝穴在足背，肝經的俞穴。"},
]


for _chunk in CHUNKS:
    _chunk["chars"] = len(_chunk["text"])


def unit(values):
    vector = np.asarray(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


VECTORS = [unit([1, 0, 0, 0]), unit([0, 1, 0, 0]), unit([0, 0, 1, 0]), unit([0, 0, 0, 1])]


def build(tmp_path, chunks=CHUNKS, vectors=VECTORS):
    path = tmp_path / "chunks.jsonl"
    path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks), encoding="utf-8")
    build_db(path, tmp_path / "index.sqlite", log=lambda m: None)
    if vectors is not None:
        np.save(tmp_path / "embeddings.f16.npy", np.stack(vectors).astype(np.float16))
    return tmp_path


class FakeEmbedder:
    def __init__(self, vector=None, error=None):
        self.vector, self.error, self.calls = vector, error, []

    def embed(self, texts, task_type, titles=None):
        self.calls.append((texts, task_type))
        if self.error:
            raise self.error
        return [(list(self.vector), 5, False)]


def test_index_tokens_are_bigrams_plus_last_char_on_search_key():
    assert index_tokens("桂枝湯，汗。B12") == ["桂枝", "枝汤", "汤", "汗", "b12"]
    assert match_query("桂枝湯") == '"桂枝" OR "枝汤"'
    assert match_query("汗") == '"汗"*'
    assert match_query("桂枝湯的組成") == '"桂枝" OR "枝汤" OR "组成"'
    assert match_query("，。") is None


def test_bm25_bigram_and_single_char_queries(tmp_path):
    store = IndexStore(build(tmp_path))
    rows = [row for row, _ in store.bm25("桂枝湯")]
    assert rows[0] == 0
    # 正體、簡體查詢都找得到（search_key 統一）
    assert [row for row, _ in store.bm25("麻黄汤")][0] == 1
    # 單字：在字串中間（出汗很多）、字串結尾（無汗而喘的「汗」在中間；「汗。」在結尾）都要找得到
    assert {row for row, _ in store.bm25("汗")} == {1, 2}
    assert store.bm25("汗", kind="document") and all(r in (1, 2) for r, _ in store.bm25("汗", kind="document"))
    assert store.bm25("汗", kind="transcript") == []
    scores = [score for _, score in store.bm25("太衝穴 肝經")]
    assert scores == sorted(scores, reverse=True) and scores[0] > 0
    assert store.bm25("。") == []


def test_vectors_are_memmapped_and_ranked(tmp_path):
    store = IndexStore(build(tmp_path))
    assert isinstance(store.vectors, np.memmap)
    top = store.vector_top([0.1, 0.9, 0.2, 0.0], limit=2)
    assert [row for row, _ in top] == [1, 2]
    assert top[0][1] == pytest.approx(0.9 / np.linalg.norm([0.1, 0.9, 0.2]), abs=1e-2)
    assert [row for row, _ in store.vector_top([1, 1, 1, 1], limit=10, kind="transcript")] in ([0, 3], [3, 0])


def test_rrf_merge_matches_formula():
    merged = rrf_merge({"bm25": [5, 7], "vector": [7, 9]}, k=60)
    scores = {row: score for row, score, _ in merged}
    assert scores[7] == pytest.approx(1 / 62 + 1 / 61)
    assert scores[5] == pytest.approx(1 / 61) and scores[9] == pytest.approx(1 / 62)
    assert [row for row, _, _ in merged] == [7, 5, 9]
    assert merged[0][2] == {"bm25": 2, "vector": 1}


def test_hybrid_search_combines_both_rankings(tmp_path):
    embedder = FakeEmbedder(vector=[0, 0, 0, 1])
    searcher = Searcher(build(tmp_path), embedder)
    result = searcher.search("桂枝湯", k=3)
    assert result["mode"] == "hybrid" and result["vector_error"] is None
    assert embedder.calls == [(["桂枝湯"], "RETRIEVAL_QUERY")]
    ids = [r["id"] for r in result["results"]]
    assert ids[:2] in (["a", "d"], ["d", "a"])           # BM25 第一名與向量第一名
    first = result["results"][0]
    assert {"rrf", "bm25", "vector", "bm25_rank", "vector_rank", "text", "title", "start"} <= set(first)
    only_docs = searcher.search("汗", kind="document")["results"]
    assert {r["kind"] for r in only_docs} == {"document"}


def test_falls_back_to_bm25_when_vertex_fails(tmp_path):
    searcher = Searcher(build(tmp_path), FakeEmbedder(error=TimeoutError("逾時")))
    result = searcher.search("桂枝湯")
    assert result["mode"] == "bm25" and "逾時" in result["vector_error"]
    assert result["results"][0]["id"] == "a" and result["results"][0]["vector"] is None


def test_missing_or_mismatched_vectors_fall_back(tmp_path):
    result = Searcher(build(tmp_path, vectors=None), FakeEmbedder([1, 0, 0, 0])).search("桂枝湯")
    assert result["mode"] == "bm25" and result["vector_error"] == "沒有向量檔"
    other = tmp_path / "other"
    other.mkdir()
    embedder = FakeEmbedder([1, 0, 0, 0])
    result = Searcher(build(other, vectors=VECTORS[:3]), embedder).search("桂枝湯")
    assert result["mode"] == "bm25" and "3 列" in result["vector_error"] and embedder.calls == []


def test_vector_meta_must_match_chunks(tmp_path):
    build(tmp_path)
    (tmp_path / "embeddings.meta.json").write_text(json.dumps({"chunks_sha256": "0" * 64}), encoding="utf-8")
    result = Searcher(tmp_path, FakeEmbedder([1, 0, 0, 0])).search("桂枝湯")
    assert result["mode"] == "bm25" and "同一份" in result["vector_error"]


def test_citation_formats():
    assert citation({**CHUNKS[0]}) == "人紀・傷寒論 傷寒論1（1） 01:05–03:10"
    assert citation({**CHUNKS[3]}) == "人紀・針灸 針灸1（1） 1:00:00–1:02:05"
    assert citation({**CHUNKS[1]}) == "人紀《傷寒論》 辨太陽病 第 12–13 頁"
    assert citation({**CHUNKS[2]}) == "事實評論一 2008-08-01"


def test_search_cli_bm25_only(tmp_path, capsys):
    from scripts import search_index

    build(tmp_path)
    assert search_index.main(["桂枝湯", "-k", "2", "--index-dir", str(tmp_path), "--bm25-only"]) == 0
    output = capsys.readouterr().out
    assert "只用關鍵字搜尋" in output
    assert "1. 人紀・傷寒論 傷寒論1（1） 01:05–03:10" in output
    assert "桂枝湯是五味藥" in output
    assert search_index.main(["不存在的詞彙", "--index-dir", str(tmp_path), "--bm25-only"]) == 1
