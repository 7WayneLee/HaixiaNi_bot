"""Antigravity 批次校正的切段、提示詞、對齊與驗收。"""

import hashlib
import inspect
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from haixia.textnorm import to_traditional
from haixia.transcript import CORRECTED_SCHEMA, course_for

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "data/correction_rules.md"
TERMS = ROOT / "data/tcm_terms_tw.txt"
COURSES = ROOT / "data/course_prompts.json"
LINE = re.compile(r"^\[(\d+(?:\.\d+)?)\]\s*(.*)$")
BATCH_NOTE = ("注意：這是批次作業。只有方名、藥名、穴位、書名、人名拿不準時，才用網路搜尋"
              "（search_web）查證，每段最多 {max_search} 次；不要拿整句話去搜尋現成的逐字稿。"
              "不能開啟網頁全文、執行指令或讀寫檔案。查不到就依聲音與上下文判斷。"
              "前文和後文只供參考，不要輸出。最後只輸出「要校正的行」校正後的結果。")
PROMPT_HEAD = ("你是倪海廈課程逐字稿的繁體中文校正員。課程：{course}。\n"
               "請依聲音、ASR 原文與上下文校正同音錯字。每一行對應輸入的一行，保留行首時間標記，"
               "行數必須完全相同；只輸出校正後的行，不加說明。")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def prompt_sha256():
    """包含提示詞範本、規則、詞表與課程熱詞設定。"""
    digest = hashlib.sha256()
    for content in (PROMPT_HEAD, BATCH_NOTE, inspect.getsource(build_prompt),
                    RULES.read_text(encoding="utf-8"),
                    TERMS.read_text(encoding="utf-8"), COURSES.read_text(encoding="utf-8")):
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def split_chunks(segments, target_sec=240, max_lines=150, context_sec=60):
    """在原 segment 邊界切約四分鐘，回傳索引與前後文索引。"""
    chunks = []
    start = 0
    while start < len(segments):
        end = start + 1
        while end < len(segments) and end - start < max_lines and segments[end]["start"] - segments[start]["start"] < target_sec:
            end += 1
        first, last = segments[start]["start"], segments[end - 1]["end"]
        before = [i for i in range(start) if segments[i]["end"] >= first - context_sec]
        after = [i for i in range(end, len(segments)) if segments[i]["start"] <= last + context_sec]
        chunks.append({"start_index": start, "end_index": end, "before": before,
                       "after": after, "start": first, "end": last})
        start = end
    return chunks


def markers(segments):
    """優先一位小數；撞號時增加精度，完全同時也給不同標記。"""
    used = set()
    result = []
    for segment in segments:
        value = segment["start"]
        for places in range(1, 7):
            candidate = f"{value:.{places}f}"
            if candidate not in used:
                break
        else:
            number = 1
            while True:
                candidate = f"{value + number / 1000000:.6f}"
                if candidate not in used:
                    break
                number += 1
        used.add(candidate)
        result.append(candidate)
    return result


def build_prompt(document, chunk, max_search=5, prompts=None):
    prompts = prompts or json.loads(COURSES.read_text(encoding="utf-8"))
    course = course_for(document["source"], prompts)
    segments = document["segments"]
    target = segments[chunk["start_index"]:chunk["end_index"]]
    labels = markers(target)
    context = lambda indices: "\n".join(f'[{segments[i]["start"]:.1f}] {segments[i]["text"]}' for i in indices)
    terms = [term.strip() for term in TERMS.read_text(encoding="utf-8").splitlines() if term.strip()]
    blocks = [PROMPT_HEAD.format(course=course["name"]), "", RULES.read_text(encoding="utf-8").strip(),
              "", "## 中醫詞表", "、".join(terms),
              "", "## 本課程常見詞", "、".join(course.get("hotwords", [])),
              "", "## 前文（只供參考，不要輸出）", context(chunk["before"]),
              "", f"## 要校正的行（共 {len(target)} 行）"]
    blocks.extend(f'[{label}] {segment["text"]}' for label, segment in zip(labels, target))
    blocks.extend(["", "## 後文（只供參考，不要輸出）", context(chunk["after"]), "",
                   BATCH_NOTE.format(max_search=max_search)])
    return "\n".join(blocks), labels


def parse_and_align(output, labels):
    """先按標記；若行數吻合且每行相差不逾一秒，再依位置補齊。"""
    lines = []
    for raw in output.splitlines():
        match = LINE.match(raw.strip())
        if match:
            lines.append((match.group(1), match.group(2).strip()))
    found = {}
    for label, text in lines:
        if label not in found:
            found[label] = text
    aligned = [found.get(label) for label in labels]
    if len(lines) == len(labels) and all(abs(float(lines[i][0]) - float(labels[i])) <= 1.0 for i in range(len(labels))):
        for i, value in enumerate(aligned):
            if value is None:
                aligned[i] = lines[i][1]
    return aligned


def _length(value):
    return len(re.sub(r"\s+", "", value))


def evaluate(output, labels, segments):
    aligned = parse_and_align(output, labels)
    matched = sum(text is not None for text in aligned)
    original = [item["text"] for item in segments]
    asr_length = sum(map(_length, original))
    corrected_length = sum(_length(text or "") for text in aligned)
    ratio = corrected_length / asr_length if asr_length else (1.0 if not corrected_length else float("inf"))
    anomalous_indices = []
    for index, (before, after) in enumerate(zip(original, aligned)):
        if after is None:
            continue
        before_length, after_length = _length(before), _length(after)
        stripped_fillers = re.sub(r"[嗯啊哈呃哦噢唉欸呀耶、，。！？!?\s]", "", before)
        if (not after and _length(stripped_fillers) >= 4) or after_length > max(2 * before_length, before_length + 20):
            anomalous_indices.append(index)
    count = len(labels)
    match_ratio = matched / count if count else 1.0
    abnormal_ratio = len(anomalous_indices) / count if count else 0.0
    return {"aligned": aligned, "matched": matched, "match_ratio": match_ratio,
            "length_ratio": ratio, "anomalous": len(anomalous_indices),
            "anomalous_indices": anomalous_indices, "abnormal_ratio": abnormal_ratio,
            "admissible": 0.6 <= ratio <= 1.5 and abnormal_ratio <= 0.05,
            "valid": matched == count and 0.6 <= ratio <= 1.5 and abnormal_ratio <= 0.05}


def choose_result(attempts, segments):
    """只從字數與異常比例合格的嘗試選取；異常行沿用 ASR。"""
    eligible = [item for item in attempts if item["evaluation"]["admissible"]]
    best = next((item for item in eligible if item["evaluation"]["valid"]), None)
    if best is None:
        best = max(eligible, key=lambda item: item["evaluation"]["matched"], default=None)
    if best is None:
        return "failed", [(item["text"], False) for item in segments]
    score = best["evaluation"]
    status = "ok" if score["valid"] else "partial" if score["match_ratio"] >= 0.95 else "failed"
    if status == "failed":
        return status, [(item["text"], False) for item in segments]
    anomalous = set(score["anomalous_indices"])
    return status, [(to_traditional(text).strip(), True) if text is not None and index not in anomalous
                    else (item["text"], False)
                    for index, (item, text) in enumerate(zip(segments, score["aligned"]))]


def corrected_document(original, chunks, results, model, max_search, digest):
    document = {key: value for key, value in original.items() if key != "segments"}
    document["schema"] = CORRECTED_SCHEMA
    document["segments"] = [dict(segment, text_asr=segment["text"], corrected=False)
                            for segment in original["segments"]]
    metadata = []
    for chunk, result in zip(chunks, results):
        for index, (text, corrected) in enumerate(result["lines"], chunk["start_index"]):
            document["segments"][index]["text"] = text
            document["segments"][index]["corrected"] = corrected
        detail = {key: result[key] for key in ("start", "end", "status", "attempts", "searches", "elapsed_sec")}
        detail["fallback_lines"] = sum(not corrected for _, corrected in result["lines"])
        detail["engine"] = result.get("engine", "antigravity-cli")
        detail["model"] = result.get("model", model)
        detail["effort"] = result.get("effort")
        metadata.append(detail)
    engines = {item["engine"] for item in metadata}
    tool = next(iter(engines)) if len(engines) == 1 else "mixed" if engines else "antigravity-cli"
    document["correction"] = {"tool": tool, "model": model,
                              "max_search": max_search, "created_at": now(),
                              "prompt_sha256": digest, "chunks": metadata}
    return document
