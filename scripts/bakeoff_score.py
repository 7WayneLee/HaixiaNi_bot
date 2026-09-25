#!/usr/bin/env python3
"""比較小規模轉錄的字錯率、中醫詞召回率及速度。"""

import argparse
import csv
import json
import re
import sys
import unicodedata
from array import array
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from haixia.textnorm import search_key  # noqa: E402


SETTINGS = (
    "whisper-prompt",
    "whisper-noprompt",
    "whisper-batched",
    "sensevoice",
    "paraformer",
)
TIME_MARK = re.compile(r"^\s*\[(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?\]\s*")


def normalize(text):
    """保留統一表意文字、擴充 A 與英數字。"""
    key = search_key(unicodedata.normalize("NFKC", text))
    output = []
    for char in key:
        if 0x3400 <= ord(char) <= 0x4DBF or 0x4E00 <= ord(char) <= 0x9FFF:
            output.append(char)
        elif "A" <= char <= "Z":
            output.append(char.lower())
        elif "a" <= char <= "z" or "0" <= char <= "9":
            output.append(char)
    return "".join(output)


def edit_counts(reference, hypothesis):
    """回溯 Levenshtein 路徑，回傳（替換、刪除、插入）。"""
    width = len(hypothesis) + 1
    matrix = [array("I", range(width))]
    for row_number, ref_char in enumerate(reference, 1):
        previous = matrix[-1]
        row = array("I", [row_number])
        for column, hyp_char in enumerate(hypothesis, 1):
            row.append(
                min(
                    previous[column - 1] + (ref_char != hyp_char),
                    previous[column] + 1,
                    row[column - 1] + 1,
                )
            )
        matrix.append(row)

    substitutions = deletions = insertions = 0
    row, column = len(reference), len(hypothesis)
    while row or column:
        current = matrix[row][column]
        if row and column and current == matrix[row - 1][column - 1] + (reference[row - 1] != hypothesis[column - 1]):
            substitutions += reference[row - 1] != hypothesis[column - 1]
            row -= 1
            column -= 1
        elif row and current == matrix[row - 1][column] + 1:
            deletions += 1
            row -= 1
        else:
            insertions += 1
            column -= 1
    return substitutions, deletions, insertions


def read_reference(path):
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(TIME_MARK.sub("", line))
    return "\n".join(lines)


def selected_segments(transcript, score_start, score_duration):
    end = score_start + score_duration
    return [
        segment
        for segment in transcript.get("segments", [])
        if score_start <= (float(segment["start"]) + float(segment["end"])) / 2 < end
    ]


def course_for(source, prompts):
    matches = [course for course in prompts.get("courses", []) if source.startswith(course["prefix"])]
    return max(matches, key=lambda course: len(course["prefix"]), default=prompts.get("default", {}))


def term_map(terms_path, course):
    terms = []
    for line in terms_path.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    terms.extend(course.get("hotwords", []))
    normalized = {}
    for term in terms:
        key = normalize(term)
        if key:
            normalized.setdefault(key, term)
    return normalized


def count_terms(text, terms):
    """每個位置取最長詞，已取用的字不再計數。"""
    trie = {}
    end_mark = ""
    for term in terms:
        node = trie
        for char in term:
            node = node.setdefault(char, {})
        node[end_mark] = term
    counts = Counter()
    index = 0
    while index < len(text):
        node = trie
        best_term = None
        best_end = index
        cursor = index
        while cursor < len(text) and text[cursor] in node:
            node = node[text[cursor]]
            cursor += 1
            if end_mark in node:
                best_term, best_end = node[end_mark], cursor
        if best_term is None:
            index += 1
        else:
            counts[best_term] += 1
            index = best_end
    return counts


def score_one(clip, transcript, reference, terms):
    start = float(clip["score_start"])
    duration = float(clip["score_duration"])
    segments = selected_segments(transcript, start, duration)
    ref_key = normalize(reference)
    hyp_key = normalize("".join(segment.get("text", "") for segment in segments))
    substitutions, deletions, insertions = edit_counts(ref_key, hyp_key)
    ref_count = count_terms(ref_key, terms)
    hyp_count = count_terms(hyp_key, terms)
    term_reference = sum(ref_count.values())
    term_matched = sum(min(count, hyp_count[term]) for term, count in ref_count.items())
    missed = {terms[term]: count - hyp_count[term] for term, count in ref_count.items() if count > hyp_count[term]}
    audio_duration = transcript.get("clip")
    if audio_duration is not None:
        audio_duration = audio_duration.get("duration")
    if audio_duration is None:
        audio_duration = transcript["duration_sec"]
    elapsed = transcript.get("engine", {}).get("elapsed_sec")
    return {
        "cer": (substitutions + deletions + insertions) / len(ref_key) if ref_key else None,
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "reference_chars": len(ref_key),
        "term_recall": term_matched / term_reference if term_reference else None,
        "term_reference": term_reference,
        "term_matched": term_matched,
        "missed_terms": missed,
        "rtf": float(elapsed) / float(audio_duration) if elapsed is not None and float(audio_duration) > 0 else None,
        "low_confidence_ratio": sum(bool(segment.get("low_confidence")) for segment in segments) / len(segments) if segments else None,
        "scored_segments": len(segments),
    }


def summarize(clips):
    summary = {}
    for setting in SETTINGS:
        rows = [clip["settings"][setting] for clip in clips if clip["settings"][setting] is not None]
        chars = sum(row["reference_chars"] for row in rows)
        errors = sum(row["substitutions"] + row["deletions"] + row["insertions"] for row in rows)
        term_reference = sum(row["term_reference"] for row in rows)
        term_matched = sum(row["term_matched"] for row in rows)
        rtfs = [row["rtf"] for row in rows if row["rtf"] is not None]
        missed = Counter()
        for row in rows:
            missed.update(row["missed_terms"])
        summary[setting] = {
            "clips_scored": len(rows),
            "cer": errors / chars if chars else None,
            "reference_chars": chars,
            "term_recall": term_matched / term_reference if term_reference else None,
            "term_reference": term_reference,
            "term_matched": term_matched,
            "mean_rtf": sum(rtfs) / len(rtfs) if rtfs else None,
            "missed_terms": dict(sorted(missed.items(), key=lambda item: (-item[1], item[0]))),
        }
    return summary


def _percent(value):
    return "—" if value is None else f"{value:.2%}"


def _decimal(value):
    return "—" if value is None else f"{value:.3f}"


def render_report(report):
    lines = ["# 小規模轉錄比較評分", "", "只評分有指定評分區間的片段；CER 越低越好，召回率越高越好。", ""]
    for clip in report["clips"]:
        lines.extend([
            f"## {clip['label']}（{clip['course']}）",
            "",
            f"來源：`{clip['source']}`；評分區間：{clip['score_start']:g}–{clip['score_start'] + clip['score_duration']:g} 秒。",
            "",
            "| 設定 | CER | 替換／刪除／插入 | 參考字數 | 中醫詞召回率 | RTF | 低信心比例 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for setting in SETTINGS:
            row = clip["settings"][setting]
            if row is None:
                status = "（缺參考答案）" if clip["reference_missing"] else "（缺）"
                lines.append(f"| {setting} | {status} | {status} | {status} | {status} | {status} | {status} |")
            else:
                counts = f"{row['substitutions']}／{row['deletions']}／{row['insertions']}"
                lines.append(f"| {setting} | {_percent(row['cer'])} | {counts} | {row['reference_chars']} | {_percent(row['term_recall'])} | {_decimal(row['rtf'])} | {_percent(row['low_confidence_ratio'])} |")
        lines.append("")
    lines.extend([
        "## 總表",
        "",
        "CER 依參考字數加權；平均 RTF 為有結果片段的算術平均。",
        "",
        "| 設定 | 已評分片段 | 加權 CER | 整體中醫詞召回率 | 平均 RTF | 漏掉最多的詞 |",
        "|---|---:|---:|---:|---:|---|",
    ])
    for setting in SETTINGS:
        row = report["summary"][setting]
        if not row["clips_scored"]:
            lines.append(f"| {setting} | 0 | （缺） | （缺） | （缺） | （缺） |")
            continue
        top = list(row["missed_terms"].items())[:5]
        missing = "、".join(f"{term}（{count}）" for term, count in top) if top else "—"
        lines.append(f"| {setting} | {row['clips_scored']} | {_percent(row['cer'])} | {_percent(row['term_recall'])} | {_decimal(row['mean_rtf'])} | {missing} |")
    return "\n".join(lines) + "\n"


def build_report(clips_path, results_path, references_path, terms_path, prompts_path):
    prompts = json.loads(prompts_path.read_text(encoding="utf-8"))
    clips = []
    with clips_path.open(encoding="utf-8", newline="") as source:
        for clip in csv.DictReader(source, delimiter="\t"):
            if not clip["score_start"].strip():
                continue
            course = course_for(clip["source"], prompts)
            terms = term_map(terms_path, course)
            reference_path = references_path / f"{clip['label']}.txt"
            reference = read_reference(reference_path) if reference_path.is_file() else None
            entry = {
                "label": clip["label"],
                "source": clip["source"],
                "course": course.get("name", "其他"),
                "score_start": float(clip["score_start"]),
                "score_duration": float(clip["score_duration"]),
                "reference_missing": reference is None,
                "settings": {},
            }
            for setting in SETTINGS:
                result = results_path / setting / f"{clip['label']}.json"
                entry["settings"][setting] = score_one(clip, json.loads(result.read_text(encoding="utf-8")), reference, terms) if reference is not None and result.is_file() else None
            clips.append(entry)
    return {"clips": clips, "summary": summarize(clips)}


def main():
    parser = argparse.ArgumentParser(description="小規模轉錄比較評分")
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--terms", type=Path, default=Path(__file__).resolve().parent.parent / "data/tcm_terms_tw.txt")
    parser.add_argument("--prompts", type=Path, default=Path(__file__).resolve().parent.parent / "data/course_prompts.json")
    args = parser.parse_args()
    report = build_report(args.clips, args.results, args.references, args.terms, args.prompts)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_report(report), encoding="utf-8")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
