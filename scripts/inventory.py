#!/usr/bin/env python3
"""盤點資料夾：統計檔案類型與大小，並用 ffprobe 算出影音總時數。

用法：
    python3 scripts/inventory.py <資料夾> [--out manifest.csv] [--workers 8] [--no-probe]

輸出：
- 終端機摘要：各類型的檔案數、大小、時數；各子資料夾的時數；可能重複的檔案
- manifest.csv：每個檔案一列，之後的轉錄步驟會用到
"""

import argparse
import csv
import json
import subprocess
import sys
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CATEGORIES = {
    "video": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg",
              "ts", "mts", "rmvb", "rm", "3gp", "vob", "f4v"},
    "audio": {"mp3", "m4a", "wav", "aac", "flac", "ogg", "oga", "wma", "amr", "opus", "ape"},
    "subtitle": {"srt", "ass", "ssa", "vtt", "sub", "lrc"},
    "pdf": {"pdf"},
    "document": {"doc", "docx", "txt", "rtf", "odt", "md", "html", "htm",
                 "ppt", "pptx", "xls", "xlsx", "csv"},
    "ebook": {"epub", "mobi", "azw", "azw3"},
    "image": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz"},
}
EXT_TO_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
LABELS = {
    "video": "影片", "audio": "音訊", "subtitle": "字幕檔", "pdf": "PDF",
    "document": "文件", "ebook": "電子書", "image": "圖片", "archive": "壓縮檔",
    "other": "其他",
}
MEDIA = {"video", "audio"}
ROOT_LABEL = "（最上層）"
FIELDS = ["path", "top_folder", "category", "ext", "size_bytes",
          "duration_sec", "has_subtitle_stream", "error"]


def probe(path):
    """回傳 (時長秒數或 None, 是否內含字幕軌, 錯誤訊息)。"""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
           "-of", "json", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return None, False, "ffprobe 逾時"
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        message = lines[-1].removeprefix(f"{path}: ") if lines else f"ffprobe 結束碼 {result.returncode}"
        return None, False, message
    try:
        info = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None, False, "ffprobe 輸出無法解析"
    try:
        duration = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    has_sub = any(s.get("codec_type") == "subtitle" for s in info.get("streams", []))
    return duration, has_sub, "" if duration else "讀不到時長"


def scan(root):
    rows = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts) or not path.is_file():
            continue
        ext = path.suffix.lower().lstrip(".")
        rows.append({
            "path": str(rel),
            "top_folder": rel.parts[0] if len(rel.parts) > 1 else ROOT_LABEL,
            "category": EXT_TO_CATEGORY.get(ext, "other"),
            "ext": ext,
            "size_bytes": path.stat().st_size,
            "duration_sec": None,
            "has_subtitle_stream": False,
            "error": "",
        })
    return rows


def probe_all(root, rows, workers):
    media = [r for r in rows if r["category"] in MEDIA]
    if not media:
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe, root / r["path"]): r for r in media}
        for done, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            row["duration_sec"], row["has_subtitle_stream"], row["error"] = future.result()
            print(f"\r已檢查影音檔 {done}/{len(media)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)


def width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text, n, right=False):
    space = " " * max(n - width(text), 0)
    return space + text if right else text + space


def gb(size):
    return f"{size / 1024**3:.1f} GB"


def hours(seconds):
    return f"{seconds / 3600:.1f} 小時" if seconds else "-"


def print_table(headers, rows, widths):
    print("  ".join(pad(h, w, right=i > 0) for i, (h, w) in enumerate(zip(headers, widths))))
    for row in rows:
        print("  ".join(pad(str(c), w, right=i > 0) for i, (c, w) in enumerate(zip(row, widths))))


def summarize(rows, probed):
    by_cat = defaultdict(lambda: [0, 0, 0.0])
    by_folder = defaultdict(lambda: [0, 0, 0.0])
    for r in rows:
        for bucket in (by_cat[r["category"]], by_folder[r["top_folder"]]):
            bucket[0] += 1
            bucket[1] += r["size_bytes"]
            bucket[2] += r["duration_sec"] or 0

    total_size = sum(r["size_bytes"] for r in rows)
    total_media = sum(r["duration_sec"] or 0 for r in rows)
    print(f"\n=== 盤點結果：共 {len(rows)} 個檔案，{gb(total_size)}", end="")
    print(f"，影音共 {hours(total_media)} ===\n" if probed else " ===\n")

    order = [c for c in LABELS if c in by_cat]
    print_table(["類型", "檔案數", "大小", "時數"],
                [[LABELS[c], by_cat[c][0], gb(by_cat[c][1]), hours(by_cat[c][2])] for c in order],
                [8, 8, 10, 12])

    print("\n--- 各資料夾（依大小排序）---")
    folders = sorted(by_folder.items(), key=lambda kv: -kv[1][1])
    name_w = min(max(width(name) for name, _ in folders), 40)
    print_table(["資料夾", "檔案數", "大小", "影音時數"],
                [[name, v[0], gb(v[1]), hours(v[2])] for name, v in folders],
                [name_w, 8, 10, 12])

    # 大小完全相同的大檔案，幾乎可以確定是同一個檔案的複本
    groups = defaultdict(list)
    for r in rows:
        if r["size_bytes"] > 1024**2 and r["category"] in MEDIA | {"pdf", "ebook"}:
            groups[(r["category"], r["size_bytes"])].append(r)
    dups = [g for g in groups.values() if len(g) > 1]
    if dups:
        extra_sec = sum((g[0]["duration_sec"] or 0) * (len(g) - 1) for g in dups)
        extra_size = sum(g[0]["size_bytes"] * (len(g) - 1) for g in dups)
        print(f"\n--- 可能重複（大小完全相同）：{len(dups)} 組，多出 {gb(extra_size)}"
              f"{'、' + hours(extra_sec) if extra_sec else ''} ---")
        for g in dups[:10]:
            print("  " + "  ＝  ".join(r["path"] for r in g))
        if len(dups) > 10:
            print(f"  ……其餘 {len(dups) - 10} 組請看 manifest")

    notes = []
    if by_cat.get("subtitle"):
        notes.append(f"有 {by_cat['subtitle'][0]} 個字幕檔：對應的影片可以直接用字幕，不必轉錄")
    with_subs = sum(1 for r in rows if r["has_subtitle_stream"])
    if with_subs:
        notes.append(f"有 {with_subs} 個影片內含字幕軌：可以直接抽出字幕")
    if by_cat.get("archive"):
        notes.append(f"有 {by_cat['archive'][0]} 個壓縮檔：裡面的檔案沒被統計，需要先解壓縮")
    errors = [r for r in rows if r["error"]]
    if errors:
        notes.append(f"有 {len(errors)} 個影音檔讀不到時長（見 manifest 的 error 欄）")
    if notes:
        print("\n--- 注意 ---")
        for note in notes:
            print("  " + note)


def main():
    parser = argparse.ArgumentParser(description="盤點資料夾內的檔案類型、大小與影音時數")
    parser.add_argument("root", type=Path, help="要盤點的資料夾")
    parser.add_argument("--out", type=Path, default=Path("manifest.csv"), help="輸出的 CSV 路徑")
    parser.add_argument("--workers", type=int, default=8, help="同時執行的 ffprobe 數量")
    parser.add_argument("--no-probe", action="store_true", help="不讀時長，只統計檔案數與大小")
    args = parser.parse_args()

    if not args.root.is_dir():
        sys.exit(f"找不到資料夾：{args.root}")

    print(f"掃描 {args.root} ……", file=sys.stderr)
    rows = scan(args.root)
    if not rows:
        sys.exit("資料夾裡沒有檔案")
    if not args.no_probe:
        probe_all(args.root, rows, args.workers)

    # utf-8-sig：Mac 的 Excel 打開中文才不會變亂碼
    with args.out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({**r, "duration_sec": f"{r['duration_sec']:.1f}" if r["duration_sec"] else ""})

    summarize(rows, probed=not args.no_probe)
    print(f"\n明細已寫入 {args.out}")


if __name__ == "__main__":
    main()
