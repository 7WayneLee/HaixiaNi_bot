"""切段、去重、資料範圍與標題清理；測試資料全部自己編，不用真實醫案。"""

import pytest

from haixia import corpus
from haixia.chunking import (ParagraphDeduper, chunk_document, chunk_transcript, dedupe_key,
                             find_date, heading_level, split_sentences)


def seg(start, text):
    return {"start": start, "end": start + 2.0, "text": text}


# ---------- 逐字稿切段 ----------

def test_transcript_chunks_keep_segments_whole_and_overlap():
    segments = [seg(i * 2.0, f"第{i:03d}句" + "甲" * 25) for i in range(100)]   # 每句 30 字
    chunks = chunk_transcript(segments, "影片/05 傷寒論/伤寒论6（2）.rmvb", "人紀・傷寒論", "傷寒論6（2）")
    texts = {s["text"] for s in segments}
    for chunk in chunks:
        pieces = chunk["text"].split(" ")
        assert all(piece in texts for piece in pieces), "不能切斷 segment"
        assert 400 <= chunk["chars"] or chunk is chunks[-1]
        assert chunk["chars"] <= 600 + 30
        assert chunk["kind"] == "transcript" and chunk["episode"] == "傷寒論6（2）"
    # 時間點是第一句起點到最後一句終點
    first = chunks[0]["text"].split(" ")
    assert chunks[0]["start"] == 0.0
    assert chunks[0]["end"] == (len(first) - 1) * 2.0 + 2.0
    # 相鄰兩段重疊約 100 字（這裡每句 30 字，所以重疊 4 句 = 120 字）
    for previous, current in zip(chunks, chunks[1:]):
        prev_pieces, cur_pieces = previous["text"].split(" "), current["text"].split(" ")
        overlap = [p for p in cur_pieces if p in prev_pieces]
        assert overlap == prev_pieces[-len(overlap):]
        assert 90 <= sum(len(p) for p in overlap) <= 150
        assert current["start"] < previous["end"]
    # 全部 segment 都有被涵蓋
    covered = {piece for chunk in chunks for piece in chunk["text"].split(" ")}
    assert covered == texts
    assert len({c["id"] for c in chunks}) == len(chunks)


def test_transcript_long_segment_is_not_cut_and_progress_is_made():
    segments = [seg(0, "長" * 900), seg(3, "短句一"), seg(5, "短句二")]
    chunks = chunk_transcript(segments, "s", "t", "e")
    assert chunks[0]["text"] == "長" * 900
    assert chunks[-1]["end"] == 7.0
    assert "短句二" in chunks[-1]["text"]


def test_transcript_skips_empty_segments_and_keeps_punctuation_joins():
    segments = [seg(0, "好，"), seg(2, "  "), seg(4, "桂枝湯")]
    chunk, = chunk_transcript(segments, "s", "t", "e")
    assert chunk["text"] == "好，桂枝湯"
    assert chunk["start"] == 0.0 and chunk["end"] == 6.0


# ---------- 文件切段 ----------

def test_document_chunks_track_pages_and_split_long_paragraphs():
    long = "".join(f"第{i}句講桂枝湯的用法。" for i in range(80))          # 約 900 字的單一段落
    paragraphs = [(1, "第一章 總論"), (1, "甲" * 300 + "。"), (2, "乙" * 300 + "。"), (3, long), (4, "結尾。")]
    chunks = chunk_document(paragraphs, "書.pdf", "人紀《傷寒論》")
    assert all(c["chars"] <= 700 for c in chunks)
    assert chunks[0]["page_start"] == 1 and chunks[0]["section"] == "第一章 總論"
    assert any(c["page_start"] == 2 and c["page_end"] == 3 for c in chunks)   # 跨頁的段落記起訖頁
    # 長段落只在句尾切開
    for chunk in chunks:
        if "第" in chunk["text"] and "句講" in chunk["text"]:
            assert chunk["text"].rstrip().endswith("。")
    assert chunks[-1]["page_end"] == 4
    assert "結尾。" in chunks[-1]["text"]           # 短尾段併進前一段
    assert len({c["id"] for c in chunks}) == len(chunks)


def test_split_sentences_hard_splits_runaway_sentence():
    parts = split_sentences("甲" * 1300 + "。乙。", max_chars=600)
    assert [len(p) for p in parts] == [600, 600, 101, 2]


def test_document_sections_dates_and_short_heading_merge():
    paragraphs = [(None, "2008年3月5日"), (None, "病人甲發熱惡寒。" * 40),
                  (None, "2008年3月9日"), (None, "病人乙口渴。" * 40), (None, "一、小結"), (None, "好。")]
    chunks = chunk_document(paragraphs, "日誌.doc", "診療日誌", track_dates=True)
    assert [c["date"] for c in chunks[:2]] == ["2008-03-05", "2008-03-09"]
    assert chunks[0]["text"].startswith("2008年3月5日")    # 只有日期的短段併進下一段
    assert chunks[-1]["text"].endswith("好。")


def test_document_drops_tiny_leftovers():
    assert chunk_document([(None, "評論")], "a.doc", "a") == []


@pytest.mark.parametrize("text, level", [
    ("第三章 十二經", 1), ("辨太陽病脈證並治法上篇", 1), ("痙濕暍病脈證治法第二", 1),
    ("生氣通天論篇第三", 1), ("壹、上經", 1), ("【附錄】", 1), ("【中央社】", 0), ("【TVBS新聞】", 0),
    ("一、丹砂", 2), ("十六：太陽病，頭痛", 2), ("（八）脈數滑者,必下血", 2), ("1、中極穴", 2),
    ("這是一般的句子，沒有編號", 0), ("一" * 50, 0),
])
def test_heading_levels(text, level):
    assert heading_level(text) == level


@pytest.mark.parametrize("text, expected", [
    ("Chen,Mary20080825乳癌", "2008-08-25"), ("04/04/2008一位病人", "2008-04-04"),
    ("2006年09月01日", "2006-09-01"), ("2009年8月", "2009-08"), ("2008/2008-04-05.htm", "2008-04-05"),
    ("沒有日期", None), ("20081399", None),
])
def test_find_date(text, expected):
    assert find_date(text) == expected


# ---------- 去重 ----------

def test_paragraph_dedupe_uses_search_key_and_ignores_punctuation():
    deduper = ParagraphDeduper()
    assert deduper.keep("桂枝湯主治太陽中風，汗出惡風。")
    assert not deduper.keep("桂枝汤主治太阳中风 汗出恶风")          # 簡繁與標點不同也算重複
    assert deduper.keep("短句")                                     # 不到 8 字不去重
    assert deduper.keep("短句")
    assert dedupe_key("A，b。 c") == "abc"


def test_sentence_coverage_catches_reflowed_paragraphs():
    deduper = ParagraphDeduper()
    assert deduper.keep("第一句講的是桂枝湯的組成。第二句講的是麻黃湯的組成。")
    assert deduper.keep("第三句講的是小柴胡湯的用法。")
    # PDF 分段不同：同樣的句子被接在一起
    assert deduper.check("第二句講的是麻黃湯的組成。第三句講的是小柴胡湯的用法。") == "句子重複"
    # 只有一小部分重複的段落要保留
    assert deduper.check("第一句講的是桂枝湯的組成。後面是全新的內容，說明煎煮方法與服用的禁忌。還有很多新的句子在這裡。") is None


# ---------- 範圍規則與標題 ----------

@pytest.mark.parametrize("path, include, group, fmt", [
    ("文字資料/01.倪海厦电子书全集/人纪 《伤寒论》-(（守候诚实）淘宝店）.pdf", True, "renji", "pdf"),
    ("文字資料/01.倪海厦电子书全集/天纪  天机道-(（守候诚实）淘宝店）.pdf", True, "tianji", "ocr"),
    ("文字資料/01.倪海厦电子书全集/天纪  《天纪》-(（守候诚实）淘宝店）.pdf", True, "tianji", "pdf"),
    ("文字資料/01.倪海厦电子书全集/天纪天机道听课笔记-(（守候诚实）淘宝店）.doc", True, "tianji", "doc"),
    ("文字資料/01.倪海厦电子书全集/天纪  地脉道-(（守候诚实）淘宝店）.pdf", False, None, None),
    ("文字資料/03.倪海厦诊疗日志 医案/倪海厦08年医案358篇(神州医料库）/某案 (1).doc", True, "cases", "doc"),
    ("文字資料/03.倪海厦诊疗日志 医案/倪海厦人纪班学的诊疗医案(神州医料库）/案例.txt", True, "cases", "txt"),
    ("文字資料/03.倪海厦诊疗日志 医案/倪海厦诊疗日志08年至9月(神州医料库）.doc", True, "journals", "doc"),
    ("文字資料/03.倪海厦诊疗日志 医案/3.倪海厦诊疗日志全集（05-08年）(神州医料库）.CHM", True, "compilations", "chm"),
    ("文字資料/03.倪海厦诊疗日志 医案/倪海厦诊疗日志医案-全(（守候诚实）淘宝店）.rar", False, None, None),
    ("文字資料/03.倪海厦诊疗日志 医案/倪海厦汉唐中医医案-分杂(神州医料库）/平衡针，肩周痛.rar", False, None, None),
    ("文字資料/03.倪海厦诊疗日志 医案/倪海厦汉唐中医医案-分杂(神州医料库）/倪海厦先生医案.pdf", True, "compilations", "pdf"),
    ("文字資料/03.倪海厦诊疗日志 医案/倪海厦汉唐中医医案-分杂(神州医料库）/某医案.htm", True, "compilations", "doc"),
    ("文字資料/03.倪海厦诊疗日志 医案/倪海厦汉唐中医医案-分杂(神州医料库）/诊疗日志20060615真武汤症.doc", True, "cases", "doc"),
    ("文字資料/03.倪海厦诊疗日志 医案/倪海厦汉唐中医医案-分杂(神州医料库）/倪海厦36条真言.doc", True, "articles", "doc"),
    ("文字資料/04.倪海厦汉唐中医/倪海厦汉唐中医日志07年（（守候诚实）淘宝店doc", True, "journals", "doc"),
    ("文字資料/04.倪海厦汉唐中医/文集及医桉最新版（（守候诚实）淘宝店）.pdf", True, "compilations", "pdf"),
    ("文字資料/04.倪海厦汉唐中医/汉唐处方（（守候诚实）淘宝店）.doc", True, "articles", "doc"),
    ("文字資料/05.倪海厦事实评论/糖尿病篇.doc", True, "articles", "doc"),
    ("文字資料/07.倪海厦国学堂/国学堂-倪海厦对话梁冬MP3(（守候诚实）淘宝店）/091226梁冬对话倪海厦第一讲.Lrc", True, "lrc", "lrc"),
    ("文字資料/07.倪海厦国学堂/国学堂-刘力红感悟《身心性》(（守候诚实）淘宝店）/梁冬对话刘力红第一讲.txt", False, None, None),
    ("文字資料/07.倪海厦国学堂/国学堂-生命太美-说白伤寒论(（守候诚实）淘宝店）/伤寒论简体版.pdf", False, None, None),
    ("文字資料/MP3 人纪全/倪海厦-人纪神农本草经（二羊中医馆）/倪海厦人纪版_神农本草经.pdf", False, None, None),
    ("電子書/倪海厦人纪系列之伤寒论.pdf", False, None, None),
    ("影片/05 傷寒論/伤寒论1（1）.rmvb", False, None, None),
])
def test_scope_rules(path, include, group, fmt):
    rule = corpus.classify(path)
    assert (rule.include, rule.group, rule.fmt) == (include, group, fmt)
    if not include:
        assert rule.reason


def test_scope_rule_reasons_and_expected_duplicates():
    assert "劉力紅" in corpus.classify("文字資料/07.倪海厦国学堂/国学堂-刘力红感悟/x.doc").reason
    assert "郭生白" in corpus.classify("文字資料/07.倪海厦国学堂/国学堂-生命太美-说白伤寒论/x.pdf").reason
    assert "王文遠" in corpus.classify("文字資料/03.倪海厦诊疗日志 医案/x-分杂/平衡针.rar").reason
    assert corpus.classify("電子書/x.pdf").expect_duplicate
    assert corpus.classify("文字資料/01.倪海厦电子书全集/天纪  地脉道-x.pdf").expect_duplicate


def test_extensionless_ole_file_is_word():
    path = "文字資料/03.倪海厦诊疗日志 医案/倪海厦08年医案959篇-按人名分类(神州医料库）/某月/,"
    assert corpus.classify(path).include is False
    assert corpus.classify(path, corpus.OLE_MAGIC).fmt == "doc"


@pytest.mark.parametrize("name, title", [
    ("人纪 《伤寒论》-(（守候诚实）淘宝店）.pdf", "人紀《傷寒論》"),
    ("人纪 《金匮要略》-（（守候诚实）淘宝店））.pdf", "人紀《金匱要略》"),
    ("天纪  天机道-(（守候诚实）淘宝店）.pdf", "天紀 天機道"),
    ("倪海夏-汉唐中医方剂讲解(（守候诚实）淘宝店.doc", "倪海廈-漢唐中醫方劑講解"),
    ("倪海厦汉唐中医日志07年（（守候诚实）淘宝店doc", "倪海廈漢唐中醫日誌07年"),
    ("倪海厦诊疗日志08年至9月(神州医料库）.doc", "倪海廈診療日誌08年至9月"),
    ("倪海厦-人纪针灸二羊中医馆", "倪海廈-人紀針灸"),
    ("事实评论一.doc", "事實評論一"),
])
def test_clean_title(name, title):
    assert corpus.clean_title(name) == title


def test_title_repairs_big5_mojibake_and_falls_back_to_folder():
    fake = "病歷".encode("big5").decode("gbk")          # 模擬 Big5 被當成 GBK 的檔名
    folder = "文字資料/03.倪海厦诊疗日志 医案/倪海厦08年医案959篇-按人名分类(神州医料库）"
    assert corpus.title_for(f"{folder}/月份/{fake}20080101.doc") == "病歷20080101"
    assert corpus.title_for(f"{folder}/{'師臨'.encode('big5').decode('gbk')}/,") == "師臨"
    assert "［檔名已隱藏" in corpus.safe_path(f"{folder}/月份/王小明.doc", "0123456789abcdef")
    assert "王小明" not in corpus.safe_path(f"{folder}/月份/王小明.doc")


def test_decode_txt_tries_encodings_in_order():
    assert corpus.decode_txt("桂枝".encode("utf-8-sig")) == "桂枝"
    assert corpus.decode_txt("桂枝".encode("utf-16")) == "桂枝"
    assert corpus.decode_txt("桂枝汤".encode("gb18030")) == "桂枝汤"
    assert corpus.decode_txt("桂枝湯".encode("big5")) in {"桂枝湯", "桂枝湯".encode("big5").decode("gb18030")}


def test_boilerplate_and_ocr_paragraphs():
    assert corpus.is_boilerplate("根据相关法律,本电子版,仅供网络测试")
    assert corpus.is_boilerplate("第 12 页") and corpus.is_boilerplate("VIII") and corpus.is_boilerplate("———")
    assert corpus.is_boilerplate("第一章 總論 ··········· 12")
    assert not corpus.is_boilerplate("第一章 總論")
    assert not corpus.is_boilerplate("他說……………………然後病人就好了，" + "這是很長的內文。" * 20)
    text = "這是第一行很長很長很長的文字\n接著第二行也很長很長很長\n結束。\n新段落開始很長很長很長的\n完。"
    assert corpus.ocr_paragraphs(text) == ["這是第一行很長很長很長的文字接著第二行也很長很長很長結束。",
                                           "新段落開始很長很長很長的完。"]


def make_pdf(path, pages, rotate=0, two_up=False):
    import pymupdf

    document = pymupdf.open()
    for page_lines in pages:
        page = document.new_page(width=595, height=842)
        page.insert_text((250, 40), "頁首書名", fontname="china-t", fontsize=10)
        y = 100
        for line in page_lines:
            page.insert_text((60, y), line, fontname="china-t", fontsize=11)
            if two_up:
                page.insert_text((330, y), "右" + line, fontname="china-t", fontsize=11)
            y += 16
        page.insert_text((280, 800), f"第 {len(document)} 頁", fontname="china-t", fontsize=9)
        if rotate:
            page.set_rotation(rotate)
    document.save(str(path))


def test_pdf_pages_removes_headers_and_keeps_page_numbers(tmp_path):
    path = tmp_path / "book.pdf"
    make_pdf(path, [["第一章 總論", "桂枝湯主治太陽中風。"], ["麻黃湯主治太陽傷寒。"], ["小結。"], ["附錄。"]])
    pages = corpus.pdf_pages(path)
    assert [number for number, _ in pages] == [1, 2, 3, 4]
    text = "".join(p for _, paragraphs in pages for p in paragraphs)
    assert "頁首書名" not in text and "頁" not in text.replace("頁首", "")
    assert pages[1][1] == ["麻黃湯主治太陽傷寒。"]


def test_pdf_two_up_columns_are_read_left_then_right(tmp_path):
    path = tmp_path / "twoup.pdf"
    make_pdf(path, [[f"左欄第一行{c}。", f"左欄第二行{c}。"] for c in "甲乙丙丁"], two_up=True)
    _, paragraphs = corpus.pdf_pages(path)[0]
    joined = "".join(paragraphs)
    assert joined.index("左欄第二行甲") < joined.index("右左欄第一行甲")
