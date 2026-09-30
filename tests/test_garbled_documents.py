"""抽取亂碼的測試只用自造文字與位元組。"""

import json

from haixia import corpus
from scripts import build_index
from scripts.lrc_to_transcript import parse_lrc
from tests.test_build_index import raw, run_chunks, write


def test_symbol_font_mapping():
    assert corpus.SYMBOL_FONT_CHARS == {"\uf0e0": "→", "\uf0e8": "⇒", "\uf0df": "←",
                                        "\uf04a": "☺", "\uf0b2": "•"}
    assert corpus.clean_extracted_text("甲\uf0e0乙\uf0e8丙\uf0df丁\uf04a\uf0b2") == "甲→乙⇒丙←丁☺•"


def test_unknown_private_chars_and_replacement_become_one_space():
    assert corpus.clean_extracted_text("甲\ue126\ue0ed\ufffd 乙") == "甲 乙"


def test_clean_text_without_private_chars_is_unchanged():
    original = " \t　甲  乙\xa0\t\n　丙  丁　 "
    assert corpus.clean_extracted_text(original) == original


def test_unknown_private_chars_only_change_their_own_position():
    assert corpus.clean_extracted_text("甲\ue126\ufffd乙") == "甲 乙"
    assert corpus.clean_extracted_text("甲 \ue126乙") == "甲 乙"
    assert corpus.clean_extracted_text("甲\ue126　乙") == "甲　乙"
    assert corpus.clean_extracted_text("\ue126甲\n乙\ufffd\n\ue0ed丙\ufffd") == "甲\n乙\n丙"


def test_normal_chinese_punctuation_is_not_garbage():
    assert not corpus.is_garbage("他說……然後——繼續·這段正常文字。" * 5)
    assert corpus.is_garbage("gMOÜOÍÅScpÑvsñﬁﬂ\uf8ff" * 10)


def test_corrupt_word_detection_requires_both_signals():
    assert not corpus.is_garbage("正常中文內容。" * 20)
    assert not corpus.is_garbage("École naïve Ελληνικά" * 10 + "中文內容" * 20)
    assert corpus.is_garbage("ÃÛﬁ∑\uf8ff" * 20)


def test_utf16_word_recovery_removes_fields_and_preserves_order():
    fake = ("\x00\r第一段虛構中文內容足夠長。\x07"
            "\x13 HYPERLINK \\l \"_Toc123\" \x14第二段虛構中文內容。\x15\x0b"
            "\x13 PAGEREF _Toc123 \\h \x14第三段虛構中文內容。\r"
            "SHORT\rENGLISH ONLY PARAGRAPH\r")
    paragraphs, info = corpus.recover_word_utf16(fake.encode("utf-16le"))
    assert paragraphs == ["第一段虛構中文內容足夠長。", "第二段虛構中文內容。", "第三段虛構中文內容。"]
    assert info == {"recovered_chars": sum(map(len, paragraphs)), "field_codes_removed": 2}


def test_cached_raw_text_is_cleaned_before_indexing(tmp_path, raw):
    path = write(raw / "文字資料/05.倪海厦事实评论/符號測試.doc", b"fake doc")
    md5 = corpus.md5_file(path)
    out = tmp_path / "out"
    write(out / f"cache/text/{md5}.json", json.dumps("第一段\uf0e0第二段\ue126內容。"))
    _, _, report, chunks = run_chunks(tmp_path, raw)
    text = "".join(c["text"] for c in chunks if c["title"] == "符號測試")
    assert "第一段→第二段 內容。" in text
    assert not report["repaired_word_files"]
    assert json.loads((out / f"cache/text/{md5}.json").read_text()) == "第一段\uf0e0第二段\ue126內容。"


def test_chunks_skip_whitespace_only_paragraph_after_cleaning(tmp_path, raw, monkeypatch):
    path = write(raw / "文字資料/05.倪海厦事实评论/空白測試.doc", b"fake doc")
    original_pages = build_index.Extractor.pages
    content = "  \t　" + "甲  乙內容足夠長。" * 15 + "　\t"

    def pages(extractor, item):
        if item["path"] == path:
            return [(None, "\ue126\t　 "), (None, content)]
        return original_pages(extractor, item)

    monkeypatch.setattr(build_index.Extractor, "pages", pages)
    _, _, report, chunks = run_chunks(tmp_path, raw)
    detail = next(item for item in report["files"] if item["path"].endswith("空白測試.doc"))
    assert detail["chars_after"] == len(content)
    assert any("甲  乙內容足夠長" in chunk["text"] for chunk in chunks)


def test_cached_corrupt_word_is_repaired_and_reported(tmp_path, raw):
    fake = ("第一段虛構中文內容，按順序保留。\r"
            "\x13 PAGEREF _Toc123 \\h \x14第二段虛構中文內容，也按順序保留。\x07")
    path = write(raw / "文字資料/05.倪海厦事实评论/修復測試.doc",
                 corpus.OLE_MAGIC + fake.encode("utf-16le"))
    md5 = corpus.md5_file(path)
    out = tmp_path / "out"
    write(out / f"cache/text/{md5}.json", json.dumps("ÃÛﬁ∑\uf8ff" * 30))
    _, _, report, chunks = run_chunks(tmp_path, raw)
    repaired = report["repaired_word_files"]
    assert len(repaired) == 1 and repaired[0]["md5"] == md5
    assert repaired[0]["field_codes_removed"] == 1
    text = "".join(c["text"] for c in chunks if c["title"] == "修復測試")
    assert text.index("第一段") < text.index("第二段")
    assert "PAGEREF" not in text and "ÃÛ" not in text
    assert json.loads((out / f"cache/text/{md5}.json").read_text()) == "ÃÛﬁ∑\uf8ff" * 30


def test_repaired_word_dedupes_after_all_other_documents(tmp_path, raw):
    folder = raw / "文字資料/05.倪海厦事实评论"
    shared = "虛構的完整原文說明桂枝湯用法，並保留正確標點。"
    healthy_paths = [write(folder / f"a0{number}完好.doc", b"fake doc " + bytes([number]))
                     for number in (1, 2)]
    out = tmp_path / "out"
    for number, path in enumerate(healthy_paths):
        content = shared if number == 0 else "另一篇完好的虛構文章，記錄不同的中醫處方。"
        write(out / f"cache/text/{corpus.md5_file(path)}.json", json.dumps(content))

    _, _, baseline_report, baseline_chunks = run_chunks(tmp_path, raw)
    repaired_path = write(folder / "a00修復.doc", corpus.OLE_MAGIC +
                          ("虛構的完整原文說明桂枝湯用法，並保留正確標點。\r"
                           "修復檔獨有的虛構內容，記錄另一種煎藥方法。\r").encode("utf-16le"))
    repaired_md5 = corpus.md5_file(repaired_path)
    write(out / f"cache/text/{repaired_md5}.json", json.dumps("ÃÛﬁ∑\uf8ff" * 30))

    _, _, report, chunks = run_chunks(tmp_path, raw)
    repaired_source = repaired_path.relative_to(raw).as_posix()
    assert report["repaired_word_files"][0]["md5"] == repaired_md5
    assert report["files"][-1]["path"] == repaired_source
    assert [item for item in report["files"] if item["path"] != repaired_source] == baseline_report["files"]
    assert [chunk for chunk in chunks if chunk["source"] != repaired_source] == baseline_chunks
    repaired_text = "".join(chunk["text"] for chunk in chunks if chunk["source"] == repaired_source)
    assert "修復檔獨有的虛構內容" in repaired_text
    assert "虛構的完整原文說明桂枝湯用法" not in repaired_text
    assert any(shared in chunk["text"] for chunk in chunks if chunk["source"] ==
               healthy_paths[0].relative_to(raw).as_posix())


def test_garbage_paragraph_is_dropped_and_counted(tmp_path, raw):
    write(raw / "文字資料/03.倪海厦诊疗日志 医案/倪海厦人纪班学的诊疗医案(神州医料库）/亂碼測試.txt",
          "ÃÛﬁ∑\uf8ff" * 25 + "\n一段虛構中文正常內容。" * 3)
    _, _, report, chunks = run_chunks(tmp_path, raw)
    assert report["groups"]["cases"]["dropped_garbage"] == 1
    assert report["dropped_garbage"] == 1
    assert all("ÃÛ" not in c["text"] for c in chunks)


def test_lrc_cleans_before_traditional_conversion():
    counts = {}
    segments = parse_lrc("[00:01.00]甲：虛構\ue126文字\uf0e0下一句\n"
                         + "[00:02.00]" + "ÃÛﬁ∑\uf8ff" * 20 + "\n", 5,
                         min_speaker_count=1, stats=counts)
    assert segments[0]["text"] == "虛構 文字→下一句"
    assert counts["dropped_garbage"] == 1


def test_lrc_line_without_private_chars_keeps_internal_spacing():
    segments = parse_lrc("[00:11.00]甲  \t　乙\n", 15, min_speaker_count=1)
    assert segments[0]["text_raw"] == "甲  \t　乙"
