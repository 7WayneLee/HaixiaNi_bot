#!/usr/bin/env python3
"""將梁冬對話字幕的 LRC 轉為逐字稿，保留說話者與時間。"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haixia.textnorm import to_traditional
from haixia.transcript import create, save

STAMP = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$")
SPEAKER = re.compile(r"^([\u3400-\u9fff]{1,12})[：:]\s*(.*)$")
URL = re.compile(r"(?:https?://|www\.|\b[a-z0-9.-]+\.(?:com|org|net|cn)\b)", re.I)
SPEAKER_FIXES = {"倪海夏": "倪海廈"}


def read_lrc(path):
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("LRC 編碼不是 UTF-8 或 GB18030")


def _name(text):
    """先轉正體，再校正原字幕常見的講者姓名錯字。"""
    name = to_traditional(text)
    return SPEAKER_FIXES.get(name, name)


def _entries(content):
    """先移除網址與製作名單；其餘文字留待頻率判斷。"""
    for raw in content.splitlines():
        match = STAMP.match(raw.strip())
        if not match:
            continue
        start = int(match[1]) * 60 + float(match[2])
        line = match[3].strip()
        if not line or URL.search(line):
            continue
        normalized = to_traditional(line)
        if normalized.startswith(("字幕製作", "義務工作群", "字幕由")):
            continue
        yield start, line


def parse_lrc(content, duration, min_speaker_count=10):
    """回傳不含引擎資訊的段落，結束時間為下一條有效字幕起點。"""
    if duration <= 0:
        raise ValueError("duration 必須大於零")
    if min_speaker_count < 1:
        raise ValueError("min_speaker_count 必須大於零")
    entries = list(_entries(content))
    counts = Counter(_name(match[1]) for _, line in entries
                     if (match := SPEAKER.match(line)))
    speakers = {name for name, count in counts.items() if count >= min_speaker_count}
    items = []
    speaker = None
    for start, line in entries:
        name = SPEAKER.match(line)
        if not items and speaker is None and name is None and (start <= 10 or re.match(r"^\d{6,}", line)):
            # 片頭沒有說話者的標題不算逐字稿。
            continue
        if name and _name(name[1]) in speakers:
            speaker, line = _name(name[1]), name[2].strip()
        if not line:
            continue
        items.append((start, line, speaker))
    result = []
    for index, (start, text, who) in enumerate(items):
        end = items[index + 1][0] if index + 1 < len(items) else float(duration)
        if end < start or end > duration + 0.1:
            raise ValueError("LRC 字幕時間超出音檔範圍或順序錯誤")
        result.append({"start": start, "end": end, "text_raw": text,
                       "text": to_traditional(text), "speaker": who,
                       "confidence": None, "low_confidence": False})
    return result


def convert(path, source, duration, out, min_speaker_count=10):
    segments = parse_lrc(read_lrc(path), duration, min_speaker_count)
    engine = {"name": "lrc", "model": "lrc", "version": "1", "params": {},
              "device": "cpu", "compute_type": "none", "elapsed_sec": 0.0}
    document = create(source, duration, engine, segments)
    save(document, out)
    return document


def main():
    parser = argparse.ArgumentParser(description="將 LRC 字幕轉成逐字稿 JSON")
    parser.add_argument("lrc", type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--min-speaker-count", type=int, default=10,
                        help="標籤至少出現幾次才視為說話者（預設 10）")
    args = parser.parse_args()
    try:
        document = convert(args.lrc, args.source, args.duration, args.out,
                           args.min_speaker_count)
    except (OSError, ValueError) as error:
        parser.exit(1, f"轉換失敗：{error}\n")
    print(f"完成 {len(document['segments'])} 段：{args.out}")


if __name__ == "__main__":
    main()
