#!/usr/bin/env python3
"""從 manifest 挑選影片畫面，按資料夾產生供人工檢查字幕的總覽圖。

用法：
    python3 scripts/sample_frames.py <媒體根目錄> --manifest manifest.csv \
        --out <輸出資料夾> [--group-depth 2] [--videos-per-group 3] \
        [--times 0.2,0.5,0.8] [--width 480] [--workers 2]

index.csv 每格一列，row 與 column 從 1 開始。失敗的格子以黑色佔位。
"""

import argparse
import csv
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath


FRAME_TIMEOUT = 180
COMPOSE_TIMEOUT = 120
INDEX_FIELDS = ["overview_file", "group", "row", "column", "video_path", "seconds", "error"]
ROOT_LABEL = "（最上層）"


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必須是正整數")
    return number


def parse_times(value):
    try:
        times = [float(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("時間比例必須是逗號分隔的數字") from exc
    if not times or any(not math.isfinite(t) or not 0 <= t < 1 for t in times):
        raise argparse.ArgumentTypeError("時間比例必須介於 0（含）與 1（不含）之間")
    return times


def read_manifest(path, group_depth):
    groups = defaultdict(list)
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = {"path", "category", "duration_sec"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"manifest 缺少欄位：{', '.join(sorted(required - set(reader.fieldnames or [])))}")
        for row in reader:
            if row["category"] != "video" or not row["duration_sec"].strip():
                continue
            try:
                duration = float(row["duration_sec"])
            except ValueError:
                continue
            if not math.isfinite(duration) or duration <= 0:
                continue
            relative = row["path"]
            if not relative:
                continue
            folders = PurePosixPath(relative).parts[:-1][:group_depth]
            group = "/".join(folders) or ROOT_LABEL
            groups[group].append((relative, duration))
    return {group: sorted(videos, key=lambda video: video[0]) for group, videos in groups.items()}


def choose_videos(videos, count):
    if len(videos) <= count:
        return videos
    if count == 1:
        return [videos[(len(videos) - 1) // 2]]
    return [videos[i * (len(videos) - 1) // (count - 1)] for i in range(count)]


def safe_filename(group):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", group).strip(" .") or "未命名"
    # 留足空間給序號及副檔名，也避免中文檔名超出檔案系統的位元組限制。
    while len(name.encode("utf-8")) > 180:
        name = name[:-1]
    return name


def ffmpeg(command, timeout):
    # 壞檔可能大量輸出錯誤訊息；暫存 stderr 在磁碟上，避免吃掉 VM 記憶體。
    with tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=errors,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return f"ffmpeg 逾時（{timeout} 秒）"
        except OSError as exc:
            return f"無法執行 ffmpeg：{exc}"
        if result.returncode == 0:
            return ""
        end = errors.tell()
        errors.seek(max(0, end - 2048))
        message = errors.read().decode("utf-8", errors="replace").strip().splitlines()
        detail = message[-1].strip() if message else f"結束碼 {result.returncode}"
        return f"ffmpeg：{detail[:300]}"


def sample_video(root, video, row_number, times, width, height, work_dir):
    relative, duration = video
    relative_path = PurePosixPath(relative)
    invalid = relative_path.is_absolute() or ".." in relative_path.parts
    source = root.joinpath(*relative_path.parts) if not invalid else None
    results = []
    for column, ratio in enumerate(times, 1):
        seconds = duration * ratio
        frame = work_dir / f"frame_{(row_number - 1) * len(times) + column - 1:04d}.jpg"
        if invalid:
            error = "manifest 的影片路徑必須位於媒體根目錄內"
        else:
            # -ss 必須放在 -i 前面；掛載的遠端大檔才能快速跳轉。
            command = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-threads", "1", "-filter_threads", "1", "-ss", f"{seconds:.3f}",
                "-i", str(source), "-map", "0:v:0", "-an", "-sn",
                "-vf", (f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                        f"flags=fast_bilinear,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"),
                "-frames:v", "1", "-q:v", "3", "-pix_fmt", "yuvj444p",
                "-update", "1", str(frame),
            ]
            error = ffmpeg(command, FRAME_TIMEOUT)
            if not error and (not frame.is_file() or frame.stat().st_size == 0):
                error = "ffmpeg 沒有輸出畫面"
        results.append({"seconds": f"{seconds:.3f}", "error": error, "frame": frame})
    return results


def make_blank(path, width, height):
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=1",
        "-frames:v", "1", "-q:v", "3", "-pix_fmt", "yuvj444p",
        "-update", "1", str(path),
    ]
    error = ffmpeg(command, COMPOSE_TIMEOUT)
    if error or not path.is_file():
        raise RuntimeError(f"無法製作空白畫面：{error or 'ffmpeg 沒有輸出畫面'}")


def compose(work_dir, output, columns, rows):
    pending = output.with_name(output.stem + ".part.jpg")
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-filter_threads", "1", "-framerate", "1",
        "-start_number", "0", "-i", str(work_dir / "frame_%04d.jpg"),
        "-vf", f"tile={columns}x{rows}:nb_frames={columns * rows}:padding=0:margin=0",
        "-frames:v", "1", "-q:v", "3", "-pix_fmt", "yuvj444p",
        "-update", "1", str(pending),
    ]
    try:
        error = ffmpeg(command, COMPOSE_TIMEOUT)
        if not error and (not pending.is_file() or pending.stat().st_size == 0):
            error = "ffmpeg 沒有輸出總覽圖"
        if error:
            return error
        os.replace(pending, output)
        return ""
    finally:
        pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="抽取影片畫面，按課程產生字幕檢查總覽圖")
    parser.add_argument("root", type=Path, help="媒體根目錄")
    parser.add_argument("--manifest", type=Path, required=True, help="inventory.py 產生的 manifest.csv")
    parser.add_argument("--out", type=Path, required=True, help="總覽圖與 index.csv 的輸出資料夾")
    parser.add_argument("--group-depth", type=positive_int, default=2, help="分組使用的資料夾層數（預設 2）")
    parser.add_argument("--videos-per-group", type=positive_int, default=3, help="每組抽取的影片數（預設 3）")
    parser.add_argument("--times", type=parse_times, default=[0.2, 0.5, 0.8], help="影片時長的截圖比例（預設 0.2,0.5,0.8）")
    parser.add_argument("--width", type=positive_int, default=480, help="每格寬度，像素（預設 480）")
    parser.add_argument("--workers", type=positive_int, default=2, help="同時讀取的影片數（預設 2）")
    args = parser.parse_args()

    if not args.root.is_dir():
        parser.error(f"找不到媒體根目錄：{args.root}")
    if not args.manifest.is_file():
        parser.error(f"找不到 manifest：{args.manifest}")
    if shutil.which("ffmpeg") is None:
        parser.error("找不到 ffmpeg，請先安裝")
    try:
        groups = read_manifest(args.manifest, args.group_depth)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        parser.error(f"無法讀取 manifest：{exc}")
    args.out.mkdir(parents=True, exist_ok=True)
    # 固定每格為 16:9；原畫面等比例縮放並補黑邊，確保拼圖格子完全對齊。
    height = max(2, round(args.width * 9 / 16))
    print(f"找到 {len(groups)} 組影片；每格 {args.width}×{height} 像素", file=sys.stderr)
    failed_cells = 0
    made = 0
    with tempfile.TemporaryDirectory(prefix="sample_frames_") as temp_root:
        blank = Path(temp_root) / "blank.jpg"
        if groups:
            try:
                make_blank(blank, args.width, height)
            except RuntimeError as exc:
                parser.error(str(exc))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            with (args.out / "index.csv").open("w", newline="", encoding="utf-8-sig") as file:
                writer = csv.DictWriter(file, fieldnames=INDEX_FIELDS)
                writer.writeheader()
                for group_number, group in enumerate(sorted(groups), 1):
                    selected = choose_videos(groups[group], args.videos_per_group)
                    image_name = f"{group_number:04d}_{safe_filename(group)}.jpg"
                    print(f"第 {group_number}/{len(groups)} 組：{group}（{len(selected)} 部影片）", file=sys.stderr)
                    with tempfile.TemporaryDirectory(dir=temp_root) as group_temp:
                        work_dir = Path(group_temp)
                        futures = {
                            pool.submit(sample_video, args.root, video, row_number,
                                        args.times, args.width, height, work_dir): row_number
                            for row_number, video in enumerate(selected, 1)
                        }
                        by_row = {}
                        for done, future in enumerate(as_completed(futures), 1):
                            row_number = futures[future]
                            try:
                                by_row[row_number] = future.result()
                            except Exception as exc:
                                by_row[row_number] = [
                                    {"seconds": f"{selected[row_number - 1][1] * ratio:.3f}",
                                     "error": f"處理失敗：{exc}",
                                     "frame": work_dir / f"frame_{(row_number - 1) * len(args.times) + column:04d}.jpg"}
                                    for column, ratio in enumerate(args.times)
                                ]
                            print(f"\r已截圖影片 {done}/{len(selected)}", end="", file=sys.stderr, flush=True)
                        print(file=sys.stderr)
                        entries = []
                        for row_number, (relative, _) in enumerate(selected, 1):
                            for column, result in enumerate(by_row[row_number], 1):
                                if result["error"]:
                                    failed_cells += 1
                                    shutil.copyfile(blank, result["frame"])
                                entries.append({
                                    "overview_file": image_name, "group": group,
                                    "row": row_number, "column": column,
                                    "video_path": relative, "seconds": result["seconds"],
                                    "error": result["error"],
                                })
                        error = compose(work_dir, args.out / image_name, len(args.times), len(selected))
                        if error:
                            print(f"  總覽圖合成失敗：{error}", file=sys.stderr)
                            for entry in entries:
                                entry["error"] = "; ".join(filter(None, (entry["error"], f"合成失敗：{error}")))
                        else:
                            made += 1
                            print(f"  已寫入 {args.out / image_name}", file=sys.stderr)
                        writer.writerows(entries)
                        file.flush()
    print(f"完成：{made}/{len(groups)} 張總覽圖，{failed_cells} 格截圖失敗；明細：{args.out / 'index.csv'}")


if __name__ == "__main__":
    main()
