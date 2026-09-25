"""逐字稿格式、檢查與幻聽過濾。時間均為原始媒體的秒數。"""

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from haixia.textnorm import search_key

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
            confidence = segment["confidence"]
            if confidence is None and segment["low_confidence"]:
                raise ValueError(f"{where} 沒有信心分數時 low_confidence 必須是 false")
            if engine["name"] in {"sensevoice", "paraformer", "lrc"} and confidence is not None:
                raise ValueError(f"{where} 此引擎的 confidence 必須是 null")
            if confidence is not None:
                keys = {"avg_logprob", "no_speech_prob", "compression_ratio"}
                if not isinstance(confidence, dict) or set(confidence) != keys or any(not _number(confidence[k]) for k in keys):
                    raise ValueError(f"{where} confidence 格式錯誤")
                if segment["low_confidence"] != is_low_confidence(confidence):
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


def _repeated_sentence(text):
    parts = [_compact(part) for part in re.split(r"[。！？!?；;，,、\n]+", text)]
    parts = [part for part in parts if part]
    for index in range(len(parts) - 2):
        if len(parts[index]) >= 4 and parts[index] == parts[index + 1] == parts[index + 2]:
            return True
    return False


def filter_hallucinations(segments):
    """回傳（保留段落，被移除段落）；重複句至少四字才整組移除。"""
    segments = list(segments)
    reasons = [None] * len(segments)
    keys = [_compact(s.get("text_raw", "")) for s in segments]
    for index, segment in enumerate(segments):
        for phrase in HALLUCINATIONS:
            if _compact(phrase) in keys[index]:
                reasons[index] = f"幻聽詞：{phrase}"
                break
        if reasons[index] is None and _repeated_sentence(segment.get("text_raw", "")):
            reasons[index] = "同一句連續重複三次"
    index = 0
    while index < len(segments):
        end = index + 1
        while end < len(segments) and keys[index] and keys[end] == keys[index]:
            end += 1
        if len(keys[index]) >= 4 and end - index >= 3:
            for position in range(index, end):
                reasons[position] = reasons[position] or "同一句連續重複三次"
        index = end
    kept = [segment for segment, reason in zip(segments, reasons) if reason is None]
    dropped = [{"start": segment["start"], "end": segment["end"], "text_raw": segment["text_raw"], "reason": reason}
               for segment, reason in zip(segments, reasons) if reason is not None]
    return kept, dropped


def course_for(source, prompts):
    """依來源路徑選擇最長前綴；prompts 可為讀入的物件或 JSON 路徑。"""
    if not isinstance(prompts, dict):
        with Path(prompts).open(encoding="utf-8") as file:
            prompts = json.load(file)
    matches = (course for course in prompts["courses"] if source.startswith(course["prefix"]))
    return max(matches, key=lambda course: len(course["prefix"]), default=prompts["default"])
