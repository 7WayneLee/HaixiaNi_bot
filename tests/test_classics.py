"""全用自編短文與 HTML 測經典處理，不讀私人資料。"""

import json
import sqlite3

from haixia import answer, classics, corpus
from haixia.classic_links import chinese_number, containment, ordered_best
from haixia.chunking import make_chunk
from haixia.index_store import build_db
from haixia.search import Searcher, citation
from haixia.telegram_bot import to_html
from scripts import build_index, parse_classics


def test_text_format_and_hierarchy(tmp_path, monkeypatch):
    root = tmp_path / "src"
    path = root / "傷寒論_宋本/index.txt"
    path.parent.mkdir(parents=True)
    path.write_text("""======傷寒論(宋本)======
<book>
書名=傷寒論(宋本)
品質=90%
</book>
<menu>
1|卷一
</menu>
=====卷第一=====
====辨少陽病脈證並治第九====
少陽之為病，口苦，咽乾，目眩也。

<F>
**柴胡湯方：**
柴胡<l>半斤，去苗</l>　甘草<l>三兩</l>
</F>

缺字　仍須保留。
""", encoding="utf-8")
    monkeypatch.setitem(classics.BOOKS, "傷寒論（宋本）", ["傷寒論_宋本/index.txt"])
    units, _ = classics.parse_book("傷寒論（宋本）", root)
    assert len(units) == 3
    assert units[0]["卷"] == "卷第一" and units[0]["篇"] == "辨少陽病脈證並治第九"
    assert units[0]["校對品質"] == 90 and "1|卷一" not in str(units)
    assert "柴胡（半斤，去苗）" in units[1]["顯示文字"]
    assert units[1]["原文"].startswith("<F>") and units[1]["夾注"] == ["半斤，去苗", "三兩"]
    assert "　" in units[2]["顯示文字"]


def test_html_modern_version_and_numbering(tmp_path):
    html = tmp_path / "page.html"
    html.write_text('<div id="263" data-sec="p">少陽<span data-rev="古版">之異</span>'
                    '<span data-rev="今版">之為病</span><jc-t attr-data-rev="古版-元素">舊</jc-t>，口苦。</div>',
                    encoding="utf-8")
    entries = classics.song_numbers(html)
    assert entries == [("263", "少陽之為病，口苦。")]
    units = [{"單位序號": 1, "篇": "辨少陽病", "顯示文字": "少陽之為病，口苦。", "原文": "少陽之為病，口苦。", "條號": None},
             {"單位序號": 2, "篇": "辨少陽病", "顯示文字": "不同文字。", "原文": "不同文字。", "條號": None}]
    missing = classics.assign_song_numbers(units, entries)
    assert units[0]["條號"] == "263" and units[1]["條號"] is None
    assert missing[0]["單位序號"] == 2


def test_pronunciation_glosses_are_excluded_and_reported(tmp_path, monkeypatch):
    root = tmp_path / "src"
    folder = root / "黃帝內經/素問"
    folder.mkdir(parents=True)
    (folder / "2.txt").write_text("""======卷第一======
=====上古天真論篇第一=====
正文（音一）仍然是正文（音二）。

上古天真論：徇（徐閏切）痹（必至切）

續列（音列）又一字（音字）

=====四氣調神大論篇第二=====
春三月，天地俱生。

四氣調神大論：獺（他達切）
""", encoding="utf-8")
    (folder / "3.txt").write_text("""======卷第二======
=====陰陽別論篇第三=====
脈有陰陽。

陰陽別論：淖（音淘）

陰陽別論：予（猶與也）
""", encoding="utf-8")
    monkeypatch.setattr(classics, "BOOKS", {"黃帝內經素問": ["黃帝內經/素問/2.txt", "黃帝內經/素問/3.txt"]})
    monkeypatch.setattr(parse_classics, "BOOKS", classics.BOOKS)
    report = parse_classics.parse_all(root, tmp_path / "parsed")
    units = json.loads((tmp_path / "parsed/黃帝內經素問.json").read_text(encoding="utf-8"))
    assert [unit["顯示文字"] for unit in units] == ["正文（音一）仍然是正文（音二）。", "春三月，天地俱生。", "脈有陰陽。"]
    assert [unit["單位序號"] for unit in units] == [1, 2, 3]
    assert report["黃帝內經素問"]["排除音釋段數"] == 5
    assert report["黃帝內經素問"]["音釋各卷"] == {"卷第一": 3, "卷第二": 2}


def test_point_and_drug_are_units(tmp_path, monkeypatch):
    for name, rel, body in [
        ("針灸大成", "針灸大成/6.txt", "======卷六======\n=====手太陰經穴主治=====\n====考正穴法====\n__列缺__\n\n定位。\n\n主咳嗽。\n\n__太淵__\n\n主胸痹。"),
        ("神農本草經", "神農本草經/index.txt", "======神農本草經======\n=====上經=====\n====柴胡====\n味苦。\n\n主寒熱。"),
    ]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        monkeypatch.setitem(classics.BOOKS, name, [rel])
        units, _ = classics.parse_book(name, tmp_path)
        assert len(units) == (2 if name == "針灸大成" else 1)
        assert "\n" in units[0]["顯示文字"]
        assert units[0]["校對品質"] == 0
        assert units[0]["篇"].endswith("列缺" if name == "針灸大成" else "柴胡")


def test_chunk_does_not_cross_section_and_citation():
    base = {"書名": "傷寒論（宋本）", "卷": "卷第五", "校對品質": 90, "夾注": []}
    units = [{**base, "篇": "辨少陽病脈證並治第九", "單位序號": 1, "條號": "263",
              "顯示文字": "少陽之為病，口苦，咽乾，目眩也。", "原文": "少陽之為病，口苦，咽乾，目眩也。"},
             {**base, "篇": "辨太陰病脈證並治", "單位序號": 2, "條號": None,
              "顯示文字": "太陰之為病，腹滿而吐。", "原文": "太陰之為病，腹滿而吐。"}]
    chunks = classics.classic_chunks(units)
    assert len(chunks) == 2 and chunks[0]["kind"] == "classic"
    assert chunks[0]["episode"] == "第263條" and chunks[0]["source"].endswith("第263條")
    assert citation(chunks[0]) == "《傷寒論（宋本）》辨少陽病脈證並治第九 第263條"
    assert chunks[0]["quality"] == 90 and chunks[0]["raw_text"] == units[0]["原文"]


def test_title_and_link_order():
    path = "文字資料/01.倪海厦电子书全集/天纪  《天纪》-(（守候诚实）淘宝店）.pdf"
    assert corpus.title_for(path) == "天紀《地脈道》"
    assert chinese_number("二七七") == 277
    assert containment("少陽之為病，口苦，咽乾，目眩", "二七七：少陽之為病口苦咽乾目眩") > .9
    route = ordered_best([(0, 2, .9, None), (1, 1, .95, None), (1, 3, .7, None), (2, 4, .8, None)])
    assert [(x[0], x[1]) for x in route] == [(0, 2), (1, 3), (2, 4)]


def test_telegram_classic_heading_is_bold():
    assert to_html("【經典原文】\n少陽之為病。") == "<b>【經典原文】</b>\n少陽之為病。"


def test_database_classic_search_and_commentary(tmp_path):
    classic = make_chunk("classic", "jicheng:傷寒論（宋本）#1-第263條", "傷寒論（宋本）",
                         "少陽之為病，口苦，咽乾，目眩也。", id="c1", section="辨少陽病脈證並治第九",
                         episode="第263條", quality=90, raw_text="少陽之為病，口苦，咽乾，目眩也。", unit_ids="[1]")
    lecture = make_chunk("document", "講義.pdf", "人紀《傷寒論》", "二七七：少陽之為病，口苦。倪師講解。", id="d1")
    video = make_chunk("transcript", "影片.rmvb", "人紀・傷寒論", "少陽之為病，口苦。", id="v1", start=30, end=50)
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (classic, lecture, video)), encoding="utf-8")
    build_db(chunks, tmp_path / "index.sqlite", log=lambda _: None)
    with sqlite3.connect(tmp_path / "index.sqlite") as connection:
        connection.executemany("INSERT INTO classic_links VALUES (?,?,?,?,?,?)", [
            ("c1", "d1", "document", .99, "條文雙字詞與順序", "277"),
            ("c1", "v1", "transcript", .8, "逐字稿雙字詞與順序", None)])
    searcher = Searcher(tmp_path)
    try:
        assert [r["id"] for r in searcher.search("少陽 口苦", kind="classic")["results"]] == ["c1"]
        args = answer.validate_input("classic_commentary", {"id": "c1"})
        text, hits = answer.run_classic_commentary(searcher.store, args, max_chars=500)
        assert "講義第277條" in text and "出處：人紀《傷寒論》" in text and len(hits) == 2
        assert len(text) <= 510
        assert answer.validate_input("search", {"query": "少陽", "kind": "classic"})["kind"] == "classic"
    finally:
        searcher.close()


def test_classic_does_not_remove_ni_document(tmp_path, monkeypatch):
    text = "少陽之為病口苦咽乾目眩，這裡加上足夠長的重複內容以便檢驗段落去重是否誤用於經典與講義。"
    raw = tmp_path / "raw"
    doc = raw / "文字資料/03.倪海厦诊疗日志 医案/倪海厦08年医案358篇/假例.txt"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    parsed = tmp_path / "parsed"
    parsed.mkdir()
    monkeypatch.setattr(build_index, "BOOKS", {"傷寒論（宋本）": ["ignored"]})
    (parsed / "傷寒論（宋本）.json").write_text(json.dumps([{
        "書名": "傷寒論（宋本）", "卷": "卷一", "篇": "辨少陽病", "單位序號": 1,
        "條號": None, "顯示文字": text, "原文": text, "夾注": [], "校對品質": 90,
    }], ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    assert build_index.main(["chunks", "--raw-dir", str(raw), "--corrected-dir", str(tmp_path / "corrected"),
                             "--classics-dir", str(parsed), "--out-dir", str(out), "--skip-rar-check"]) == 0
    rows = [json.loads(line) for line in (out / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["kind"] == "classic" and row["text"] == text for row in rows)
    assert any(row["kind"] == "document" and text in row["text"] for row in rows)
