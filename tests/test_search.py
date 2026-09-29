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
    assert citation({**CHUNKS[0]}) == "人紀・傷寒論 傷寒論1（1） 01:05–03:10（編號 a）"
    assert citation({**CHUNKS[3]}) == "人紀・針灸 針灸1（1） 1:00:00–1:02:05（編號 d）"
    assert citation({**CHUNKS[1]}) == "人紀《傷寒論》 辨太陽病 第 12–13 頁（編號 b）"
    assert citation({**CHUNKS[2]}) == "事實評論一 2008-08-01（編號 c）"


def test_search_cli_bm25_only(tmp_path, capsys):
    from scripts import search_index

    build(tmp_path)
    assert search_index.main(["桂枝湯", "-k", "2", "--index-dir", str(tmp_path), "--bm25-only"]) == 0
    output = capsys.readouterr().out
    assert "只用關鍵字搜尋" in output
    assert "1. 人紀・傷寒論 傷寒論1（1） 01:05–03:10（編號 a）" in output
    assert "桂枝湯是五味藥" in output
    assert search_index.main(["不存在的詞彙", "--index-dir", str(tmp_path), "--bm25-only"]) == 1


# ---------- 出處縮短與醫案隱藏姓名（姓名都是虛構的測試資料） ----------

from haixia.search import (CitationStore, case_info, display_source_path, original_title, repair_filename, short_section,  # noqa: E402
                           split_case_title, unique_prefix)

CASE_DIR = "文字資料/03.倪海厦诊疗日志 医案/"
FOLDER_959 = CASE_DIR + "倪海厦08年医案959篇-按人名分类(神州医料库）/倪師臨床醫案8_2008/"
# 959 篇的檔名是 Big5 位元組被當成 GB 解讀的亂碼
GARBLED = "Roe,Rick 20080915-胃痛兼失眠".encode("big5").decode("gb18030")


def doc(chunk_id, source, title, section=None, date=None, page=None, text="內文。"):
    return {"id": chunk_id, "kind": "document", "source": source, "title": title, "episode": None,
            "section": section, "page_start": page, "page_end": page, "start": None, "end": None,
            "date": date, "text": text, "chars": len(text)}


def test_short_section_rules():
    # 針灸教程：去掉時間碼；第一層太長就只留第二層
    assert short_section("第三章十二經納天干地支與十二正經井榮俞原經合（1-01:14:05） 5、脊中與筋縮穴（2-01:07:00）") \
        == "5、脊中與筋縮穴"
    assert short_section("第一章針灸的使用時機（1-00:00:14）") == "第一章針灸的使用時機"
    assert short_section("第六章 針灸治症系列(8-00:52:47)") == "第六章 針灸治症系列"
    # 條文：只留條號與開頭 10 個字
    assert short_section("辨少陽病脈證並治法 二七七：「少陽」之為病,口苦,咽乾,目眩也。") == "二七七：「少陽」之為病,口苦…"
    assert short_section("辨太陽病脈證並治法上篇 二九：服桂枝湯") == "二九：服桂枝湯"
    # 第一層短就保留，截斷第二層；總長 20 字加「…」
    assert short_section("平人氣象論篇第十八 （八）寸脈若沈而急緩不定，是往來寒熱之表現") == "平人氣象論篇第十八 （八）寸脈若沈而急緩…"
    assert short_section("辨太陽病") == "辨太陽病"
    assert short_section("【胸痺心痛短氣病脈證治第九") == "胸痺心痛短氣病脈證治第九"
    assert short_section("（辨太陽病）") == "（辨太陽病）"
    assert short_section("【正常】 【未閉") == "【正常】 未閉"
    assert short_section("一二三四五六七八九十一二三四五六七八九十一二") == "一二三四五六七八九十一二三四五六七八九十…"
    assert short_section(None) == "" and short_section("") == ""


def test_split_case_title_rules():
    assert split_case_title("Doe,Jane20080807-皮癢") == ("2008-08-07", "皮癢")
    assert split_case_title("王小明20080807-頭痛") == ("2008-08-07", "頭痛")
    assert split_case_title("Doe, Jane _20080930_腰背痛") == ("2008-09-30", "腰背痛")
    assert split_case_title("Doe_Jane20080922-2-右臉麻痺") == ("2008-09-22", "右臉麻痺")
    assert split_case_title("012 D,JX 20080414 人工心臟瓣膜") == ("2008-04-14", "人工心臟瓣膜")
    assert split_case_title("120-1 D,J 20070828~20071107-ASCITES") == ("2007-08-28", "ASCITES")
    assert split_case_title("027-1 D,B 20080422") == ("2008-04-22", "")
    # 日期前面沒有英文字母（不是姓名）、不是合法日期、沒有日期：都不算
    assert split_case_title("診療日誌20060615真武湯症") is None
    assert split_case_title("Doe,Jane20081399-皮癢") is None
    assert split_case_title("倪醫師病案紀錄 (1)") is None
    assert split_case_title("診療日誌20060615真武湯症") is None
    assert split_case_title("") is None


def test_case_info_by_folder_and_title():
    by_title = doc("x1", "文字資料/其他/Doe,Jane20080807-皮癢.doc", "Doe,Jane20080807-皮癢")
    assert case_info(by_title) == {"date": "2008-08-07", "complaint": "皮癢"}
    # 358 篇、人紀班：沒有可可靠剝除的姓名時，不公開標題；日期用段落的日期
    folder_358 = doc("x2", CASE_DIR + "倪海厦08年医案358篇(神州医料库）/倪醫師病案紀錄 (1).doc", "倪醫師病案紀錄 (1)",
                     date="2008-03-03")
    assert case_info(folder_358) == {"date": "2008-03-03", "complaint": ""}
    chinese_name = doc("x7", CASE_DIR + "倪海厦08年医案358篇/王小明-頭痛.doc", "王小明-頭痛")
    assert "王小明" not in citation(chinese_name)
    # 959 篇：用修復後的檔名（corpus 修不回來的也修得回來），轉成台灣用字
    garbled = doc("x3", FOLDER_959 + GARBLED + ".doc", "亂碼標題", date="2008-09-15")
    assert repair_filename(GARBLED) == "Roe,Rick 20080915-胃痛兼失眠"
    assert original_title(garbled) == "Roe,Rick 20080915-胃痛兼失眠"
    assert case_info(garbled) == {"date": "2008-09-15", "complaint": "胃痛兼失眠"}
    # 959 篇沒有日期：去掉開頭的英文姓名；檔名沒有內容（「,」）時不顯示主訴
    no_date_name = "Doe,Jane不孕，上熱下寒".encode("big5").decode("gb18030")
    no_date = doc("x4", FOLDER_959 + no_date_name + ".doc", "亂碼", date="2008-08-01")
    assert case_info(no_date) == {"date": "2008-08-01", "complaint": "不孕，上熱下寒"}
    assert case_info(doc("x5", FOLDER_959 + ",", "亂碼資料夾"))["complaint"] == ""
    # 主訴太長截斷
    long = doc("x6", "文字資料/x.doc", "Doe,Jane20080807-" + "很長的主訴" * 6)
    assert case_info(long)["complaint"] == ("很長的主訴" * 4) + "…"
    # 不是醫案：講義、日誌、逐字稿
    assert case_info(doc("y1", "文字資料/人紀.pdf", "人紀《傷寒論》", section="辨太陽病")) is None
    assert case_info(doc("y2", CASE_DIR + "倪海厦汉唐中医医案-分杂(神州医料库）/诊疗日志20060615真武汤症.doc",
                         "診療日誌20060615真武湯症")) is None
    assert case_info({**CHUNKS[0]}) is None


def test_case_citation_hides_name():
    record = doc("3e13af9c00000000", "文字資料/x/Doe,Jane20080807-皮癢.doc", "Doe,Jane20080807-皮癢",
                 section="1. 大便：正常")
    text = citation(record)
    assert text == "醫案 2008-08-07 皮癢（編號 3e13af）"
    assert "Doe" not in text and "Jane" not in text
    assert citation(doc("eeeeee0000000000", "文字資料/x/王小明20080807-頭痛.doc",
                        "王小明20080807-頭痛")) == "醫案 2008-08-07 頭痛（編號 eeeeee）"
    assert citation({**record, "short_id": "3e13af9"}) == "醫案 2008-08-07 皮癢（編號 3e13af9）"
    garbled = doc("aaaaaa0000000000", FOLDER_959 + GARBLED + ".doc", "亂碼")
    assert citation(garbled) == "醫案 2008-09-15 胃痛兼失眠（編號 aaaaaa）"
    assert citation(doc("bbbbbb0000000000", CASE_DIR + "倪海厦人纪班学的诊疗医案(神州医料库）/求孕.doc", "求孕")) \
        == "醫案（編號 bbbbbb）"


def test_document_citation_shortened():
    record = doc("z1", "文字資料/人紀.pdf", "人紀《傷寒論》", page=164,
                 section="辨少陽病脈證並治法 二七七：「少陽」之為病,口苦,咽乾,目眩也。")
    assert citation(record) == "人紀《傷寒論》 二七七：「少陽」之為病,口苦… 第 164 頁（編號 z1）"
    record = doc("z2", "文字資料/針灸.pdf", "人紀《針灸教程》", page=35,
                 section="第三章十二經納天干地支與十二正經井榮俞原經合（1-01:14:05） 5、脊中與筋縮穴（2-01:07:00）")
    assert citation(record) == "人紀《針灸教程》 5、脊中與筋縮穴 第 35 頁（編號 z2）"


def test_unique_prefix_grows_until_unique():
    ids = ["abcdef01", "abcdef02", "abcdee00", "123456aa"]

    def count(prefix):
        return sum(1 for chunk_id in ids if chunk_id.startswith(prefix))

    assert unique_prefix("123456aa", count) == "123456"
    assert unique_prefix("abcdee00", count) == "abcdee"
    assert unique_prefix("abcdef01", count) == "abcdef0" + "1"
    assert unique_prefix("abcdef01", count, minimum=8) == "abcdef01"


def test_citation_store_short_ids_and_prefix_lookup(tmp_path):
    chunks = [
        doc("3e13af0000000001", "文字資料/a/Doe,Jane20080807-皮癢.doc", "Doe,Jane20080807-皮癢", text="第一段。"),
        doc("3e13af1000000002", "文字資料/a/Doe,Jane20080807-皮癢.doc", "Doe,Jane20080807-皮癢", text="第二段。"),
        doc("777777a000000003", "文字資料/a/Roe,Rick20080901-胃痛.doc", "Roe,Rick20080901-胃痛", text="胃痛。"),
        doc("3e13b00000000004", "文字資料/人紀.pdf", "人紀《傷寒論》", section="辨太陽病", page=1, text="講義。"),
    ]
    store = CitationStore(build(tmp_path, chunks=chunks, vectors=None))
    records = store.rows([0, 1, 2, 3])
    # 前 6 碼相同的兩段自動加長成 7 碼；各種段落都帶編號。
    assert records[0]["short_id"] == "3e13af0" and records[1]["short_id"] == "3e13af1"
    assert records[2]["short_id"] == "777777"
    assert records[3]["short_id"] == "3e13b0"
    assert citation(records[0]) == "醫案 2008-08-07 皮癢（編號 3e13af0）"
    assert [r["id"] for r in store.find_prefix("3e13af")] == ["3e13af0000000001", "3e13af1000000002"]
    assert [r["id"] for r in store.find_prefix("777777")] == ["777777a000000003"]
    assert store.find_prefix("ffffff") == []
    # read_context（neighbors）拿到的段落也有 short_id
    target, previous, following = store.neighbors("3e13af1000000002", 1, 1)
    assert previous[0]["short_id"] == "3e13af0" and target["short_id"] == "3e13af1"
    store.close()


@pytest.mark.parametrize("number", range(6, 12))
def test_display_source_path_repairs_only_garbled_parts(number):
    basename = "Doe,Jane20080807-皮癢".encode("big5").decode("gb18030") + ".doc"
    root = CASE_DIR + "倪海厦08年医案959篇-按人名分类(神州医料库）/"
    original = root + f"畍羬洛{number}_2008(神州医料库）/" + basename
    displayed = display_source_path(original)
    assert displayed == root + f"師臨醫{number}_2008(神州医料库）/Doe,Jane20080807-皮癢.doc"
    assert display_source_path(original[:-4] + "😀.doc").endswith("Doe,Jane20080807-皮癢😀.doc")
    assert display_source_path("文字資料/正常資料夾/正常檔名.doc") == "文字資料/正常資料夾/正常檔名.doc"
    complete = root + f"畍羬洛{number}_2008(神州医料库）/" + basename
    assert display_source_path(complete) == root + f"倪師臨床醫案{number}_2008(神州医料库）/Doe,Jane20080807-皮癢.doc"
