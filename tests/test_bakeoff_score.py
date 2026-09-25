"""小規模轉錄評分的手算案例。"""

import json

import pytest

from scripts.bakeoff_score import (
    build_report,
    count_terms,
    edit_counts,
    normalize,
    read_reference,
    render_report,
    selected_segments,
)


@pytest.mark.parametrize(
    ("reference", "hypothesis", "expected"),
    [
        ("甲乙丙", "甲丁丙", (1, 0, 0)),
        ("甲乙丙", "甲丙", (0, 1, 0)),
        ("甲乙丙", "甲乙丁丙", (0, 0, 1)),
    ],
)
def test_edit_counts(reference, hypothesis, expected):
    actual = edit_counts(reference, hypothesis)
    assert actual == expected
    assert sum(actual) / len(reference) == pytest.approx(1 / 3)


def test_simple_and_variant_are_equal():
    reference = normalize("黃耆湯")
    hypothesis = normalize("黄芪汤")
    assert reference == hypothesis
    assert edit_counts(reference, hypothesis) == (0, 0, 0)


def test_punctuation_space_and_case_are_ignored():
    assert normalize(" 桂枝湯，ABC！ ") == normalize("桂枝汤abc")


def test_midpoint_selects_crossing_segments():
    transcript = {"segments": [
        {"start": 8, "end": 12, "text": "甲"},  # 中點 10，選入
        {"start": 18, "end": 22, "text": "乙"},  # 中點 20，排除
        {"start": 9, "end": 11, "text": "丙"},
    ]}
    assert [segment["text"] for segment in selected_segments(transcript, 10, 10)] == ["甲", "丙"]


def test_terms_take_longest_nonoverlapping_match():
    counts = count_terms("小柴胡汤柴胡", {"小柴胡汤": "小柴胡湯", "柴胡": "柴胡"})
    assert counts == {"小柴胡汤": 1, "柴胡": 1}


def test_reference_ignores_timestamps_and_comments(tmp_path):
    path = tmp_path / "reference.txt"
    path.write_text("# 說明\n[00:01] 桂枝湯\n[01:02:03] 黃耆湯\n  # 備註\n", encoding="utf-8")
    assert normalize(read_reference(path)) == normalize("桂枝湯黃耆湯")


def test_missing_results_and_report(tmp_path):
    clips = tmp_path / "clips.tsv"
    clips.write_text(
        "label\tsource\tstart\tduration\tscore_start\tscore_duration\n"
        "one\t影片/05 傷寒論/甲.rmvb\t0\t20\t0\t10\n"
        "skip\t影片/05 傷寒論/乙.rmvb\t0\t20\t\t\n",
        encoding="utf-8",
    )
    references = tmp_path / "references"
    references.mkdir()
    (references / "one.txt").write_text("[00:00] 小柴胡湯\n", encoding="utf-8")
    results = tmp_path / "results"
    result_dir = results / "whisper-prompt"
    result_dir.mkdir(parents=True)
    (result_dir / "one.json").write_text(json.dumps({
        "clip": {"start": 0, "duration": 20},
        "duration_sec": 20,
        "engine": {"elapsed_sec": 5},
        "segments": [
            {"start": 0, "end": 8, "text": "小柴胡湯", "low_confidence": False},
            {"start": 7, "end": 11, "text": "", "low_confidence": True},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    terms = tmp_path / "terms.txt"
    terms.write_text("小柴胡湯\n柴胡\n", encoding="utf-8")
    prompts = tmp_path / "prompts.json"
    prompts.write_text(json.dumps({"default": {"name": "其他", "hotwords": []}, "courses": [
        {"prefix": "影片/", "name": "一般", "hotwords": []},
        {"prefix": "影片/05 傷寒論/", "name": "傷寒", "hotwords": ["小柴胡湯"]},
    ]}, ensure_ascii=False), encoding="utf-8")
    report = build_report(clips, results, references, terms, prompts)
    assert len(report["clips"]) == 1
    assert report["clips"][0]["course"] == "傷寒"
    row = report["clips"][0]["settings"]["whisper-prompt"]
    assert row["cer"] == 0
    assert row["term_recall"] == 1
    assert row["rtf"] == 0.25
    assert row["low_confidence_ratio"] == 0.5
    assert report["clips"][0]["settings"]["sensevoice"] is None
    markdown = render_report(report)
    assert "（缺）" in markdown
    assert "50.00%" in markdown


def test_missing_reference_is_reported_without_interrupting(tmp_path):
    clips = tmp_path / "clips.tsv"
    clips.write_text(
        "label\tsource\tstart\tduration\tscore_start\tscore_duration\n"
        "missing\t影片/05 傷寒論/甲.rmvb\t0\t20\t0\t10\n",
        encoding="utf-8",
    )
    results = tmp_path / "results" / "whisper-prompt"
    results.mkdir(parents=True)
    (results / "missing.json").write_text("{}", encoding="utf-8")
    references = tmp_path / "references"
    references.mkdir()
    terms = tmp_path / "terms.txt"
    terms.write_text("小柴胡湯\n", encoding="utf-8")
    prompts = tmp_path / "prompts.json"
    prompts.write_text('{"default": {"name": "其他", "hotwords": []}}', encoding="utf-8")

    report = build_report(clips, results.parent, references, terms, prompts)
    assert report["clips"][0]["reference_missing"] is True
    assert report["clips"][0]["settings"]["whisper-prompt"] is None
    assert report["summary"]["whisper-prompt"]["clips_scored"] == 0
    assert "（缺參考答案）" in render_report(report)
    json.dumps(report, ensure_ascii=False)
