#!/usr/bin/env python3
"""以 ffmpeg 將影音轉成 16 kHz 單聲道 FLAC，可從 manifest 批次續跑。"""

import argparse
import csv
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

FFMPEG_TIMEOUT = 3600


def extract_audio(source, target, start=None, duration=None, timeout=FFMPEG_TIMEOUT):
    """成功轉檔回傳 True；已存在時回傳 False；失敗時拋出例外。"""
    source, target = Path(source), Path(target)
    if target.exists():
        return False
    if start is not None and start < 0 or duration is not None and duration <= 0:
        raise ValueError("片段起點必須非負，長度必須大於零")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".flac", dir=target.parent)
    os.close(descriptor)
    temp = Path(temp_name)
    try:
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if start is not None:
            command += ["-ss", str(start)]
        command += ["-i", str(source)]
        if duration is not None:
            command += ["-t", str(duration)]
        command += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", str(temp)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"ffmpeg 結束碼 {result.returncode}")
        os.replace(temp, target)
        return True
    finally:
        temp.unlink(missing_ok=True)


def _selected(path, includes, excludes):
    return (not includes or any(path.startswith(prefix) for prefix in includes)) and not any(path.startswith(prefix) for prefix in excludes)


def extract_manifest(manifest, root, out_dir, includes=(), excludes=(), workers=2, timeout=FFMPEG_TIMEOUT):
    if workers < 1:
        raise ValueError("workers 必須大於零")
    root, out_dir = Path(root), Path(out_dir)
    with Path(manifest).open(encoding="utf-8-sig", newline="") as stream:
        rows = [row for row in csv.DictReader(stream)
                if row.get("category") in {"video", "audio"} and _selected(row.get("path", ""), includes, excludes)]
    tasks = []
    for row in rows:
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts:
            print(f"略過不安全的路徑：{relative}", file=sys.stderr)
            continue
        tasks.append((root / relative, out_dir / (str(relative) + ".flac")))
    counts = {"完成": 0, "跳過": 0, "失敗": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract_audio, source, target, timeout=timeout): (source, target)
                   for source, target in tasks}
        for number, future in enumerate(as_completed(futures), 1):
            source, target = futures[future]
            try:
                status = "完成" if future.result() else "跳過"
            except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
                status = "失敗"
                print(f"轉檔失敗：{source}：{error}", file=sys.stderr)
            counts[status] += 1
            print(f"已處理 {number}/{len(tasks)}：{status} {target}", file=sys.stderr)
    return counts


def main():
    parser = argparse.ArgumentParser(description="轉成 16 kHz 單聲道 FLAC")
    parser.add_argument("source", nargs="?", type=Path, help="單檔輸入媒體")
    parser.add_argument("target", nargs="?", type=Path, help="單檔輸出 FLAC")
    parser.add_argument("--start", type=float, help="片段起點秒數")
    parser.add_argument("--duration", type=float, help="片段長度秒數")
    parser.add_argument("--manifest", type=Path, help="inventory.py 產生的 CSV")
    parser.add_argument("--root", type=Path, help="raw 目錄")
    parser.add_argument("--out-dir", type=Path, help="批次輸出目錄")
    parser.add_argument("--include", action="append", default=[], help="只處理這個路徑前綴，可重複")
    parser.add_argument("--exclude", action="append", default=[], help="略過這個路徑前綴，可重複")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.manifest:
        if not args.root or not args.out_dir or args.source or args.target:
            parser.error("批次模式需要 --root 與 --out-dir，不能給單檔路徑")
        extract_manifest(args.manifest, args.root, args.out_dir, args.include, args.exclude, args.workers)
    else:
        if not args.source or not args.target:
            parser.error("單檔模式需要輸入與輸出路徑")
        try:
            done = extract_audio(args.source, args.target, args.start, args.duration)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
            parser.exit(1, f"轉檔失敗：{error}\n")
        print(f"{'完成' if done else '已存在，跳過'}：{args.target}")


if __name__ == "__main__":
    main()
