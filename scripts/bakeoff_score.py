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
# 純語助詞；在 search_key（轉簡體）之後比對，所以誒也要列簡體的诶。
FILLERS = frozenset("嗯啊哈呃哦欸誒诶喔唉噢")
FILLER_DISPLAY = "嗯、啊、哈、呃、哦、欸、誒、喔、唉、噢"
NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
NUMBER_UNITS = ("", "十", "百", "千")
LARGE_UNITS = ("", "萬", "億", "兆", "京")
CHINESE_DIGITS = "零一二三四五六七八九"


def _four_digits(value):
    output = []
    zero = False
    for position, digit in enumerate(f"{value:04d}"):
        number = int(digit)
        if number == 0:
            zero = bool(output)
        else:
            if zero:
                output.append("零")
            output.append(CHINESE_DIGITS[number] + NUMBER_UNITS[3 - position])
            zero = False
    result = "".join(output)
    return result[1:] if result.startswith("一十") else result


def _integer_in_chinese(digits):
    value = int(digits)
    if value == 0:
        return "零"
    groups = []
    while value:
        groups.append(value % 10000)
        value //= 10000
    if len(groups) > len(LARGE_UNITS):
        return "".join(CHINESE_DIGITS[int(digit)] for digit in digits)
    output = ""
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group:
            continue
        if output and group < 1000:
            output += "零"
        output += _four_digits(group) + LARGE_UNITS[index]
    return output


def _number_in_chinese(match):
    whole, dot, fraction = match.group().partition(".")
    result = _integer_in_chinese(whole)
    return result + "點" + "".join(CHINESE_DIGITS[int(digit)] for digit in fraction) if dot else result


def normalize(text, keep_fillers=False, keep_numbers=False):
    """統一字形、症／證及數字念法；預設拿掉語助詞。"""
    text = unicodedata.normalize("NFKC", text)
    if not keep_numbers:
        text = NUMBER.sub(_number_in_chinese, text)
    key = search_key(text).replace("证", "症")
    output = []
    for char in key:
        if 0x3400 <= ord(char) <= 0x4DBF or 0x4E00 <= ord(char) <= 0x9FFF:
            if keep_fillers or char not in FILLERS:
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


def term_map(terms_path, course, keep_fillers=False, skipped=None, keep_numbers=False):
    """回傳 {正規化鍵: 原詞}。拿掉語助詞時，含語助詞用字的詞（例如呃逆）無法可靠比對，改記到 skipped。"""
    terms = []
    for line in terms_path.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    terms.extend(course.get("hotwords", []))
    normalized = {}
    for term in terms:
        key = normalize(term, keep_fillers, keep_numbers)
        if not keep_fillers and key != normalize(term, keep_fillers=True, keep_numbers=keep_numbers):
            if skipped is not None:
                skipped.add(term)
            continue
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


def score_one(clip, transcript, reference, terms, keep_fillers=False, keep_numbers=False):
    start = float(clip["score_start"])
    duration = float(clip["score_duration"])
    segments = selected_segments(transcript, start, duration)
    ref_key = normalize(reference, keep_fillers, keep_numbers)
    hyp_key = normalize("".join(segment.get("text", "") for segment in segments), keep_fillers, keep_numbers)
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


def filler_note(report):
    if report.get("keep_fillers"):
        note = "正規化：保留語助詞（`--keep-fillers`），語助詞也計入字錯率。"
    else:
        note = f"正規化：已拿掉語助詞（{FILLER_DISPLAY}），不計入字錯率與中醫詞比對；要保留請加 `--keep-fillers`。"
    skipped = report.get("skipped_terms")
    if skipped:
        note += f"下列中醫詞含語助詞用字，拿掉語助詞後無法比對，未列入召回率：{'、'.join(skipped)}。"
    if report.get("keep_numbers"):
        note += "保留阿拉伯數字（`--keep-numbers`）。"
    else:
        note += "阿拉伯數字轉為中文念法（可用 `--keep-numbers` 關閉）。"
    note += "「證」與「症」視為同一字。"
    return note


def render_report(report):
    lines = [
        "# 小規模轉錄比較評分",
        "",
        filler_note(report),
        "",
        "只評分有指定評分區間的片段；CER 越低越好，召回率越高越好。",
        "",
    ]
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


def build_report(clips_path, results_path, references_path, terms_path, prompts_path,
                 keep_fillers=False, keep_numbers=False):
    prompts = json.loads(prompts_path.read_text(encoding="utf-8"))
    clips = []
    skipped = set()
    with clips_path.open(encoding="utf-8", newline="") as source:
        for clip in csv.DictReader(source, delimiter="\t"):
            if not clip["score_start"].strip():
                continue
            course = course_for(clip["source"], prompts)
            terms = term_map(terms_path, course, keep_fillers, skipped, keep_numbers)
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
                entry["settings"][setting] = score_one(clip, json.loads(result.read_text(encoding="utf-8")), reference, terms, keep_fillers, keep_numbers) if reference is not None and result.is_file() else None
            clips.append(entry)
    return {
        "keep_fillers": keep_fillers,
        "keep_numbers": keep_numbers,
        "skipped_terms": sorted(skipped),
        "clips": clips,
        "summary": summarize(clips),
    }


def main():
    parser = argparse.ArgumentParser(description="小規模轉錄比較評分")
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--terms", type=Path, default=Path(__file__).resolve().parent.parent / "data/tcm_terms_tw.txt")
    parser.add_argument("--prompts", type=Path, default=Path(__file__).resolve().parent.parent / "data/course_prompts.json")
    parser.add_argument("--keep-fillers", action="store_true", help=f"保留語助詞（預設拿掉：{FILLER_DISPLAY}）")
    parser.add_argument("--keep-numbers", action="store_true", help="保留阿拉伯數字，不轉成中文念法")
    args = parser.parse_args()
    report = build_report(args.clips, args.results, args.references, args.terms, args.prompts,
                          args.keep_fillers, args.keep_numbers)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_report(report), encoding="utf-8")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
