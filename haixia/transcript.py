"""逐字稿格式、檢查與幻聽過濾。時間均為原始媒體的秒數。"""

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from haixia.textnorm import search_key, to_traditional

SCHEMA = "haixia.transcript/1"
HALLUCINATIONS = (
    "請不吝點讚", "訂閱", "轉發", "打賞", "字幕由", "明鏡與點點", "謝謝觀看", "Amara.org",
)


def create(source, duration_sec, engine, segments, clip=None, dropped=None):
    """建立並驗證一份逐字稿。"""
    document = {
        "schema": SCHEMA,
        "source": source,
        "clip": clip,
        "duration_sec": float(duration_sec),
        "engine": engine,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "segments": list(segments),
        "dropped": list(dropped or []),
    }
    validate(document)
    return document


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(document):
    """格式不符時拋出附繁體中文原因的 ValueError。"""
    if not isinstance(document, dict):
        raise ValueError("逐字稿必須是物件")
    required = {"schema", "source", "clip", "duration_sec", "engine", "created_at", "segments", "dropped"}
    if set(document) != required:
        raise ValueError(f"逐字稿欄位錯誤：缺少 {sorted(required - set(document))}，多出 {sorted(set(document) - required)}")
    if document["schema"] != SCHEMA:
        raise ValueError("逐字稿 schema 不正確")
    if not isinstance(document["source"], str) or not document["source"] or Path(document["source"]).is_absolute() or ".." in Path(document["source"]).parts:
        raise ValueError("source 必須是 raw 底下的相對路徑")
    duration = document["duration_sec"]
    if not _number(duration) or duration <= 0:
        raise ValueError("duration_sec 必須是正數")
    clip = document["clip"]
    base = 0.0
    if clip is not None:
        if not isinstance(clip, dict) or set(clip) != {"start", "duration"} or not _number(clip["start"]) or not _number(clip["duration"]) or clip["start"] < 0 or clip["duration"] <= 0:
            raise ValueError("clip 必須包含非負的 start 與正數 duration")
        if abs(clip["duration"] - duration) > 0.1:
            raise ValueError("clip.duration 與 duration_sec 不相符")
        base = clip["start"]
    engine = document["engine"]
    engine_fields = {"name", "model", "version", "params", "device", "compute_type", "elapsed_sec"}
    if not isinstance(engine, dict) or set(engine) != engine_fields:
        raise ValueError("engine 欄位不完整")
    for field in ("name", "model", "version", "device", "compute_type"):
        if not isinstance(engine[field], str) or not engine[field]:
            raise ValueError(f"engine.{field} 必須是非空字串")
    if engine["name"] not in {"whisper", "whisper-batched", "sensevoice", "paraformer", "lrc"}:
        raise ValueError("engine.name 不支援")
    if not isinstance(engine["params"], dict):
        raise ValueError("engine.params 必須是物件")
    if not _number(engine["elapsed_sec"]) or engine["elapsed_sec"] < 0:
        raise ValueError("engine.elapsed_sec 必須是非負數")
    try:
        datetime.fromisoformat(document["created_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("created_at 必須是 ISO 8601 時間") from None
    for field in ("segments", "dropped"):
        if not isinstance(document[field], list):
            raise ValueError(f"{field} 必須是陣列")
        previous = base
        for index, segment in enumerate(document[field], 1):
            where = f"{field} 第 {index} 段"
            if not isinstance(segment, dict):
                raise ValueError(f"{where} 必須是物件")
            expected = {"start", "end", "text_raw", "text", "speaker", "confidence", "low_confidence"} if field == "segments" else {"start", "end", "text_raw", "reason"}
            if field == "segments" and "filtered" in segment:
                expected.add("filtered")
            if set(segment) != expected:
                raise ValueError(f"{where} 欄位錯誤")
            start, end = segment["start"], segment["end"]
            if not _number(start) or not _number(end) or start < base - 0.1 or end < start or end > base + duration + 0.1:
                raise ValueError(f"{where} 時間超出音檔範圍")
            if start < previous - 0.1:
                raise ValueError(f"{where} 時間順序錯誤")
            previous = start
            if not isinstance(segment["text_raw"], str):
                raise ValueError(f"{where} text_raw 必須是字串")
            if field == "dropped":
                if not isinstance(segment["reason"], str) or not segment["reason"]:
                    raise ValueError(f"{where} reason 必須是非空字串")
                continue
            if not isinstance(segment["text"], str):
                raise ValueError(f"{where} text 必須是字串")
            if segment["speaker"] is not None and not isinstance(segment["speaker"], str):
                raise ValueError(f"{where} speaker 必須是字串或 null")
            if not isinstance(segment["low_confidence"], bool):
                raise ValueError(f"{where} low_confidence 必須是布林值")
            filtered = segment.get("filtered", False)
            if not isinstance(filtered, bool) or (filtered and not segment["low_confidence"]):
                raise ValueError(f"{where} filtered 與 low_confidence 不相符")
            confidence = segment["confidence"]
            if confidence is None and segment["low_confidence"] and not filtered:
                raise ValueError(f"{where} 沒有信心分數時 low_confidence 必須是 false，除非經過濾器改寫")
            if engine["name"] in {"sensevoice", "paraformer", "lrc"} and confidence is not None:
                raise ValueError(f"{where} 此引擎的 confidence 必須是 null")
            if confidence is not None:
                keys = {"avg_logprob", "no_speech_prob", "compression_ratio"}
                if not isinstance(confidence, dict) or set(confidence) != keys or any(not _number(confidence[k]) for k in keys):
                    raise ValueError(f"{where} confidence 格式錯誤")
                if segment["low_confidence"] != (is_low_confidence(confidence) or filtered):
                    raise ValueError(f"{where} low_confidence 與規則不符")
    return document


def save(document, path):
    """在同目錄暫存後原子取代目標檔。"""
    validate(document)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as output:
            temp_path = Path(output.name)
            json.dump(document, output, ensure_ascii=False, indent=1)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load(path):
    with Path(path).open(encoding="utf-8") as source:
        return validate(json.load(source))


def is_low_confidence(confidence):
    if confidence is None:
        return False
    return (confidence["avg_logprob"] < -1.0 or confidence["compression_ratio"] > 2.4
            or confidence["no_speech_prob"] > 0.6)


def _compact(text):
    return re.sub(r"[^\w]+", "", search_key(text).casefold(), flags=re.UNICODE)


def _compact_positions(text):
    """回傳去標點的搜尋鍵，以及每個字對應的原文起訖位置。"""
    positions = []
    for index, original in enumerate(text):
        for _ in _compact(original):
            positions.append((index, index + 1))
    return _compact(text), positions


def _prompt_leak_start(key, prompt_key):
    """找出與提示詞重疊至少八字的最早位置。"""
    if len(prompt_key) < 8:
        return None
    for start in range(len(key) - 7):
        if any(key[start:start + 8] == prompt_key[index:index + 8]
               for index in range(len(prompt_key) - 7)):
            return start
    return None


def _repetition_cut(key, positions):
    """找出三次以上的段內迴圈；回傳應移除的原文起訖位置。"""
    for start in range(len(key) - 11):
        best = None
        for length in range(4, (len(key) - start) // 3 + 1):
            phrase = key[start:start + length]
            cursor = start + length
            count = 1
            while True:
                next_start = next((cursor + gap for gap in range(3)
                                   if key.startswith(phrase, cursor + gap)), None)
                if next_start is None:
                    break
                cursor = next_start + length
                count += 1
            if count >= 3 and (best is None or (count, length) > (best[0], best[1])):
                best = count, length, cursor
        if best is not None:
            _count, length, cursor = best
            phrase = key[start:start + length]
            previous = key.rfind(phrase, max(0, start - length - 12), start)
            cut_start = start + length
            if previous >= 0 and previous + length <= start:
                prefix = 0
                while (prefix < 12 and previous - prefix > 0 and start - prefix > 0
                       and key[previous - prefix - 1] == key[start - prefix - 1]):
                    prefix += 1
                if prefix >= 4:
                    cut_start = start - prefix
            raw_start = (positions[cut_start - 1][1] if cut_start == start + length
                         else positions[cut_start][0])
            return raw_start, positions[cursor - 1][1]
    return None


def filter_hallucinations(segments, prompt=""):
    """回傳（保留段落，被移除文字）；保留段內迴圈的第一次出現。"""
    segments = list(segments)
    keys = [_compact(segment.get("text_raw", "")) for segment in segments]
    repeated_segments = set()
    index = 0
    while index < len(segments):
        end = index + 1
        while end < len(segments) and keys[index] and keys[end] == keys[index]:
            end += 1
        if len(keys[index]) >= 4 and end - index >= 3:
            repeated_segments.update(range(index, end))
        index = end

    kept, dropped = [], []
    prompt_key = _compact(prompt)
    for index, segment in enumerate(segments):
        raw = segment["text_raw"]
        key, positions = _compact_positions(raw)
        reason = next((f"幻聽詞：{phrase}" for phrase in HALLUCINATIONS
                       if _compact(phrase) in key), None)
        if reason is None and index in repeated_segments:
            reason = "同一句連續重複三次"
        if reason is not None:
            dropped.append({"start": segment["start"], "end": segment["end"],
                            "text_raw": raw, "reason": reason})
            continue

        leak_start = _prompt_leak_start(key, prompt_key)
        cut = (positions[leak_start][0], len(raw)) if leak_start is not None else None
        reason = "提示詞外洩" if cut is not None else None
        if cut is None:
            cut = _repetition_cut(key, positions)
            reason = "段內重複迴圈" if cut is not None else None
        if cut is None:
            kept.append(segment)
            continue

        start, end = cut
        dropped.append({"start": segment["start"], "end": segment["end"],
                        "text_raw": raw[start:end], "reason": reason})
        remaining = (raw[:start] + raw[end:]).strip()
        if reason == "提示詞外洩" and not _compact(remaining):
            dropped[-1]["text_raw"] = raw
        elif remaining:
            rewritten = segment.copy()
            rewritten.update(text_raw=remaining, text=to_traditional(remaining),
                             low_confidence=True, filtered=True)
            kept.append(rewritten)
    return kept, dropped


def course_for(source, prompts):
    """依來源路徑選擇最長前綴；prompts 可為讀入的物件或 JSON 路徑。"""
    if not isinstance(prompts, dict):
        with Path(prompts).open(encoding="utf-8") as file:
            prompts = json.load(file)
    matches = (course for course in prompts["courses"] if source.startswith(course["prefix"]))
    return max(matches, key=lambda course: len(course["prefix"]), default=prompts["default"])


CORRECTED_SCHEMA = "haixia.corrected/1"


def validate_corrected(document):
    """驗證校正版；原始逐字稿的 validate 行為保持不變。"""
    if not isinstance(document, dict) or document.get("schema") != CORRECTED_SCHEMA:
        raise ValueError("校正版逐字稿 schema 不正確")
    correction = document.get("correction")
    fields = {"tool", "model", "max_search", "created_at", "prompt_sha256", "chunks"}
    if not isinstance(correction, dict) or set(correction) != fields:
        raise ValueError("correction 欄位錯誤")
    if correction["tool"] not in {"antigravity-cli", "codex-cli", "mixed"} or not isinstance(correction["model"], str) or not correction["model"]:
        raise ValueError("correction 工具或模型不正確")
    if type(correction["max_search"]) is not int or correction["max_search"] < 0:
        raise ValueError("correction.max_search 必須是非負整數")
    if not isinstance(correction["prompt_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", correction["prompt_sha256"]):
        raise ValueError("correction.prompt_sha256 不正確")
    try:
        datetime.fromisoformat(correction["created_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("correction.created_at 必須是 ISO 8601 時間") from None
    if not isinstance(correction["chunks"], list):
        raise ValueError("correction.chunks 必須是陣列")
    for index, chunk in enumerate(correction["chunks"], 1):
        legacy = {"start", "end", "status", "attempts", "searches", "elapsed_sec", "fallback_lines"}
        modern = legacy | {"engine", "model", "effort"}
        if not isinstance(chunk, dict) or set(chunk) not in (legacy, modern):
            raise ValueError(f"correction.chunks 第 {index} 段欄位錯誤")
        if set(chunk) == modern and (chunk["engine"] not in {"antigravity-cli", "codex-cli"}
                                     or not isinstance(chunk["model"], str) or not chunk["model"]
                                     or chunk["effort"] is not None and not isinstance(chunk["effort"], str)):
            raise ValueError(f"correction.chunks 第 {index} 段引擎欄位錯誤")
        if (not _number(chunk["start"]) or not _number(chunk["end"]) or
                chunk["start"] < 0 or chunk["end"] < chunk["start"]):
            raise ValueError(f"correction.chunks 第 {index} 段時間錯誤")
        if chunk["status"] not in {"ok", "partial", "failed"}:
            raise ValueError(f"correction.chunks 第 {index} 段狀態錯誤")
        if (type(chunk["attempts"]) is not int or chunk["attempts"] < 0 or
                type(chunk["searches"]) is not int or chunk["searches"] < 0 or
                type(chunk["fallback_lines"]) is not int or chunk["fallback_lines"] < 0 or
                not _number(chunk["elapsed_sec"]) or chunk["elapsed_sec"] < 0):
            raise ValueError(f"correction.chunks 第 {index} 段計數錯誤")
    original = {key: value for key, value in document.items() if key != "correction"}
    original["schema"] = SCHEMA
    if not isinstance(original.get("segments"), list):
        raise ValueError("segments 必須是陣列")
    clean_segments = []
    for index, segment in enumerate(original["segments"], 1):
        if not isinstance(segment, dict) or "text_asr" not in segment or "corrected" not in segment:
            raise ValueError(f"segments 第 {index} 段缺少校正欄位")
        if not isinstance(segment["text_asr"], str) or type(segment["corrected"]) is not bool:
            raise ValueError(f"segments 第 {index} 段校正欄位錯誤")
        if not segment["corrected"] and segment.get("text") != segment["text_asr"]:
            raise ValueError(f"segments 第 {index} 段未校正文字與 ASR 不符")
        clean_segments.append({key: value for key, value in segment.items()
                               if key not in {"text_asr", "corrected"}})
    original["segments"] = clean_segments
    validate(original)
    return document


def save_corrected(document, path):
    """驗證並原子寫入校正版逐字稿。"""
    validate_corrected(document)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as output:
            temp_path = Path(output.name)
            json.dump(document, output, ensure_ascii=False, indent=1)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
