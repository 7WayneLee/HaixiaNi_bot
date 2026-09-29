#!/usr/bin/env python3
"""建索引：文字辨識（ocr）→ 切段（chunks）→ 向量（embed）→ SQLite（db）。

    ocr     在 movie-nas 執行：Cloud Vision 辨識天機道掃描檔，存成逐頁文字
    chunks  在 Mac 執行（不連網）：轉文字、去重、切段，輸出 chunks.jsonl 與 build_report.json
    embed   在 movie-nas 執行：Vertex AI gemini-embedding-001 算向量（會花錢，先 --dry-run）
    db      Mac 或 movie-nas：把 chunks 寫進 index.sqlite（內文、metadata、FTS5）

每一步都可以中斷後重跑：轉文字與向量有快取，產物先寫暫存檔再改名。
報告與 log 只印統計、路徑與原因，不印內文；以病人姓名命名的檔案只印雜湊。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haixia import corpus, vertex
from haixia.classics import BOOKS, classic_chunks
from haixia.chunking import ParagraphDeduper, chunk_document, chunk_transcript, find_date
from haixia.index_store import DB_NAME, build_db
from haixia.textnorm import to_traditional
from haixia.transcript import course_for, validate_corrected

ROOT = Path(__file__).resolve().parents[1]
COURSES = ROOT / "data/course_prompts.json"
DEFAULT_OUT = Path.home() / "haixia-index-build"
DEFAULT_RAW = Path.home() / "haixia-text-raw"
DEFAULT_CORRECTED = Path.home() / "haixia-corrected"
DEFAULT_CLASSICS = Path.home() / "haixia-classics/parsed"
TIANJIDAO = "文字資料/01.倪海厦电子书全集/天纪  天机道-(（守候诚实）淘宝店）.pdf"
LRC_TITLE = "梁冬對話倪海廈"
BIG_RAR = "倪海厦诊疗日志医案-全"
DATED_GROUPS = ("cases", "journals", "compilations")


def log(message):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


# ---------- ocr ----------

def cmd_ocr(args):
    bucket = args.bucket
    source = args.source
    pdf_uri = f"gs://{bucket}/raw/{source}"
    prefix = args.output_prefix or f"gs://{bucket}/ocr/tianjidao/"
    out_dir = Path(args.out_dir) / "ocr"
    out_dir.mkdir(parents=True, exist_ok=True)
    pages_path = out_dir / "tianjidao.pages.json"
    state_path = out_dir / "tianjidao.operation.json"
    if pages_path.exists() and not args.force:
        log(f"已有逐頁文字：{pages_path}（要重做請加 --force）")
        return 0
    api = vertex.GoogleApi(timeout=60, log=log)
    ocr = vertex.VisionOCR(api, log=log)
    out_bucket, _ = vertex.split_gcs(prefix)
    outputs = ocr.list_outputs(prefix)
    if state_path.exists() and not args.force:
        operation = json.loads(state_path.read_text(encoding="utf-8"))["operation"]
        log(f"接續等待先前送出的工作：{operation}")
        ocr.wait(operation, poll_sec=args.poll_sec)
    elif outputs and not args.force:
        log(f"{prefix} 已有 {len(outputs)} 個輸出檔，直接讀取（要重送請加 --force）")
    else:
        operation = ocr.submit(pdf_uri, prefix, language_hints=args.language_hints.split(","))
        write_atomic(state_path, json.dumps({"operation": operation, "source": source,
                                             "output_prefix": prefix, "submitted_at": now()},
                                            ensure_ascii=False, indent=1) + "\n")
        log(f"已送出 Vision 工作：{operation}")
        ocr.wait(operation, poll_sec=args.poll_sec)
    outputs = ocr.list_outputs(prefix)
    if not outputs:
        log(f"錯誤：{prefix} 底下沒有輸出檔")
        return 1
    pages = vertex.parse_vision_outputs(ocr.download_json(out_bucket, name) for name in outputs)
    write_atomic(pages_path, json.dumps({"source": source, "engine": "cloud-vision DOCUMENT_TEXT_DETECTION",
                                         "output_prefix": prefix, "created_at": now(), "pages": pages},
                                        ensure_ascii=False, indent=1) + "\n")
    empty = sum(1 for page in pages if not page["text"].strip())
    log(f"完成：{len(pages)} 頁（{empty} 頁沒有文字），存到 {pages_path}")
    if args.expect_pages and len(pages) != args.expect_pages:
        log(f"警告：預期 {args.expect_pages} 頁，實際 {len(pages)} 頁")
        return 1
    return 0


# ---------- chunks：轉文字 ----------

class Extractor:
    """轉文字並快取（快取在 out-dir，以 MD5 為檔名；不在 repo 裡）。"""

    def __init__(self, cache_dir, ocr_dir):
        self.cache = Path(cache_dir)
        self.ocr = {}
        for path in sorted(Path(ocr_dir).glob("*.pages.json")) if Path(ocr_dir).exists() else []:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.ocr[data["source"]] = data["pages"]

    def _cached(self, kind, md5, make):
        path = self.cache / kind / f"{md5}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        value = make()
        write_atomic(path, json.dumps(value, ensure_ascii=False))
        return value

    def doc_text(self, path, md5):
        return self._cached("text", md5, lambda: corpus.textutil_text(path))

    def pages(self, item):
        """回傳 [(頁碼或 None, 段落)]；CHM 另外處理。"""
        fmt, path, md5 = item["fmt"], item["path"], item["md5"]
        if fmt == "doc":
            return [(None, p) for p in corpus.text_paragraphs(self.doc_text(path, md5))]
        if fmt == "txt":
            return [(None, p) for p in corpus.text_paragraphs(corpus.decode_txt(path.read_bytes()))]
        if fmt == "pdf":
            pages = self._cached("pdf", md5, lambda: corpus.pdf_pages(path))
            return [(number, p) for number, paragraphs in pages for p in paragraphs]
        if fmt == "ocr":
            if item["rel"] not in self.ocr:
                raise LookupError("尚未做文字辨識（先在 movie-nas 跑 ocr，再把結果拉回來）")
            return [(page["page"], p) for page in self.ocr[item["rel"]]
                    for p in corpus.ocr_paragraphs(page["text"])]
        raise ValueError(f"不支援的格式 {fmt}")

    def chm_pages(self, path, md5):
        """解開 CHM，回傳 [(內部路徑, 段落清單)]，依內部路徑排序。"""
        def make():
            if not shutil.which("7zz"):
                raise LookupError("找不到 7zz（brew install sevenzip）")
            with tempfile.TemporaryDirectory() as temp:
                subprocess.run(["7zz", "x", "-y", f"-o{temp}", str(path)], check=True,
                               capture_output=True, timeout=300)
                result = []
                for page in sorted(Path(temp).rglob("*")):
                    if page.suffix.lower() in {".htm", ".html"}:
                        inner = page.relative_to(temp).as_posix()
                        result.append([inner, corpus.text_paragraphs(corpus.textutil_text(page))])
                return result
        return self._cached("chm", md5, make)


def scan_files(raw_dir):
    """列出 raw 目錄的檔案，判斷範圍並算 MD5。"""
    items = []
    for path in sorted(Path(raw_dir).rglob("*")):
        if not path.is_file() or path.name.startswith("._") or path.name == ".DS_Store":
            continue
        rel = path.relative_to(raw_dir).as_posix()
        with path.open("rb") as source:
            head = source.read(8)
        rule = corpus.classify(rel, head)
        items.append({"rel": rel, "path": path, "size": path.stat().st_size, "md5": corpus.md5_file(path),
                      "rule": rule, "group": rule.group, "fmt": rule.fmt})
    return items


def keep_order(item):
    """同一 MD5 的保留順序：組別優先序 → 不在「分杂」→ 路徑。"""
    priority = corpus.GROUPS[item["group"]][0] if item["group"] else 99
    return (priority, "分杂" in item["rel"], item["rel"])


def file_dedupe(items):
    """回傳（保留的檔案, 略過清單, 警告）。"""
    included = sorted((i for i in items if i["rule"].include), key=keep_order)
    kept, skipped, warnings = [], [], []
    first_by_md5 = {}
    for item in included:
        if item["md5"] in first_by_md5:
            skipped.append({"path": corpus.safe_path(item["rel"], item["md5"]), "md5": item["md5"],
                            "reason": f"MD5 與 {corpus.safe_path(first_by_md5[item['md5']]['rel'])} 相同"})
            continue
        first_by_md5[item["md5"]] = item
        kept.append(item)
    for item in items:
        rule = item["rule"]
        if rule.include:
            continue
        reason = rule.reason
        if rule.expect_duplicate:
            twin = first_by_md5.get(item["md5"])
            if twin:
                reason = f"{reason}（已確認 MD5 與 {corpus.safe_path(twin['rel'])} 相同）"
            else:
                reason = f"{reason}（但找不到 MD5 相同的檔案，請人工確認）"
                warnings.append(f"{corpus.safe_path(item['rel'])} 預期是重複檔，但沒有 MD5 相同的檔案")
        skipped.append({"path": corpus.safe_path(item["rel"], item["md5"]), "md5": item["md5"], "reason": reason})
    return kept, skipped, warnings


def rar_check(items, cache_dir, raw_dir, private_path):
    """解開大 RAR，逐檔以 MD5（其次檔名）比對醫案資料夾。未對到的清單只寫進 private_path。"""
    rar = next((i for i in items if BIG_RAR in i["rel"] and i["rel"].endswith(".rar")), None)
    if rar is None:
        return {"status": "找不到大 RAR"}
    if not shutil.which("unar"):
        return {"status": "找不到 unar，未比對（brew install unar）"}
    target = Path(cache_dir) / "rar" / rar["md5"]
    if not (target / ".done").exists():
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        try:
            subprocess.run(["unar", "-q", "-f", "-o", str(target), str(rar["path"])], check=True,
                           capture_output=True, timeout=600)
        except (subprocess.SubprocessError, OSError) as error:
            return {"status": f"解壓失敗，未比對（{type(error).__name__}）"}
        (target / ".done").write_text("", encoding="utf-8")
    known_md5 = {i["md5"] for i in items if i["rel"].startswith(corpus.DIR_CASES) and i is not rar}
    known_names = {PurePosixPath(i["rel"]).name for i in items if i["rel"].startswith(corpus.DIR_CASES)}
    total = matched = 0
    unmatched = []
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.name == ".done":
            continue
        total += 1
        md5 = corpus.md5_file(path)
        if md5 in known_md5:
            matched += 1
            continue
        unmatched.append({"inner": path.relative_to(target).as_posix(), "md5": md5, "size": path.stat().st_size,
                          "same_name_exists": path.name in known_names})
    if unmatched:
        write_atomic(private_path, "".join(f"{u['md5']}\t{u['size']}\t{u['same_name_exists']}\t{u['inner']}\n"
                                           for u in unmatched))
    by_ext = defaultdict(int)
    for entry in unmatched:
        by_ext[PurePosixPath(entry["inner"]).suffix.lower() or "（無副檔名）"] += 1
    return {"status": "已比對", "files": total, "md5_matched": matched, "unmatched": len(unmatched),
            "unmatched_same_name": sum(1 for u in unmatched if u["same_name_exists"]),
            "unmatched_by_ext": dict(by_ext),
            "unmatched_list": (f"{private_path.name}（在 out-dir，檔名可能含病人姓名，不要貼進 repo）"
                               if unmatched else None),
            "note": "未對到的檔案沒有自動加入索引"}


def lrc_segments(path):
    from scripts.lrc_to_transcript import STAMP, parse_lrc, read_lrc

    lines = []
    for line in read_lrc(path).splitlines():
        if match := STAMP.match(line.strip()):
            lines.append((int(match[1]) * 60 + float(match[2]), line.strip()))
    if not lines:
        raise ValueError("LRC 沒有時間標記")
    # 有幾個檔的時間標記前後顛倒（差不到 1 秒），依時間穩定排序後再解析。
    lines.sort(key=lambda entry: entry[0])
    # 沒有音檔長度；以最後一條字幕後 5 秒當結尾。
    return parse_lrc("\n".join(line for _stamp, line in lines), lines[-1][0] + 5.0)


def lrc_episode(name):
    match = re.search(r"第[一二三四五六七八九十]+讲", name)
    return to_traditional(match[0]) if match else corpus.clean_title(name)


def lrc_date(name):
    match = re.match(r"(\d{2})(\d{2})(\d{2})", name)
    return f"20{match[1]}-{match[2]}-{match[3]}" if match else None


def transcript_chunks(corrected_dir, prompts):
    for path in sorted(Path(corrected_dir).rglob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        validate_corrected(document)
        source = document["source"]
        title = course_for(source, prompts)["name"]
        episode = to_traditional(PurePosixPath(source).stem)
        chars = sum(len(segment["text"].strip()) for segment in document["segments"])
        yield source, chars, chunk_transcript(document["segments"], source, title, episode)


def cmd_chunks(args):
    started = time.time()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "cache"
    prompts = json.loads(COURSES.read_text(encoding="utf-8"))
    extractor = Extractor(cache_dir, args.ocr_dir or out_dir / "ocr")

    log("掃描文字資料並計算 MD5")
    items = scan_files(args.raw_dir)
    kept, skipped, warnings = file_dedupe(items)
    log(f"{len(items)} 個檔案：保留 {len(kept)} 個，略過 {len(skipped)} 個")

    docs = [i for i in kept if i["fmt"] == "doc"]
    log(f"用 textutil 轉 {len(docs)} 個 doc／docx／htm（有快取）")
    failures = []

    def warm(item):
        try:
            extractor.doc_text(item["path"], item["md5"])
        except Exception as error:  # noqa: BLE001
            return item, error
        return item, None

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        for item, error in executor.map(warm, docs):
            if error:
                failures.append(item)

    stats = defaultdict(lambda: {"files": 0, "paragraphs_before": 0, "chars_before": 0,
                                 "paragraphs_after": 0, "chars_after": 0, "boilerplate_paragraphs": 0,
                                 "dropped_paragraph": 0, "dropped_sentence": 0,
                                 "chunks": 0, "chunk_chars": 0})
    errors = []
    temp = out_dir / ".chunks.jsonl.tmp"
    ids = set()
    total_chunks = 0
    with temp.open("w", encoding="utf-8") as output:
        def emit(group, chunks):
            nonlocal total_chunks
            for chunk in chunks:
                base = chunk["id"]
                suffix = 1
                while chunk["id"] in ids:
                    suffix += 1
                    chunk["id"] = f"{base}-{suffix}"
                ids.add(chunk["id"])
                output.write(json.dumps(chunk, ensure_ascii=False) + "\n")
                stats[group]["chunks"] += 1
                stats[group]["chunk_chars"] += chunk["chars"]
                total_chunks += 1

        # 1. 影片逐字稿（彼此不做段落去重）
        log("切逐字稿")
        for _source, chars, chunks in transcript_chunks(args.corrected_dir, prompts):
            stats["asr"]["files"] += 1
            stats["asr"]["chars_before"] += chars
            emit("asr", chunks)

        # 經典獨立處理，不進倪師文件的段落去重器。
        classics_arg = getattr(args, "classics_dir", None)
        classics_dir = Path(classics_arg) if classics_arg else (DEFAULT_CLASSICS if out_dir == DEFAULT_OUT else None)
        if classics_dir and classics_dir.exists():
            for name in BOOKS:
                path = classics_dir / f"{name}.json"
                if not path.exists():
                    raise FileNotFoundError(f"找不到經典解析結果：{path}（先跑 scripts/parse_classics.py）")
                units = json.loads(path.read_text(encoding="utf-8"))
                chunks = classic_chunks(units)
                stats["classic"]["files"] += 1
                stats["classic"]["paragraphs_before"] += len(units)
                stats["classic"]["paragraphs_after"] += len(units)
                stats["classic"]["chars_before"] += sum(len(u["顯示文字"]) for u in units)
                stats["classic"]["chars_after"] += sum(len(u["顯示文字"]) for u in units)
                emit("classic", chunks)

        # 2. 梁冬對話 LRC
        for item in (i for i in kept if i["group"] == "lrc"):
            try:
                segments = lrc_segments(item["path"])
            except (OSError, ValueError) as error:
                errors.append({"path": item["rel"], "error": str(error)})
                continue
            name = PurePosixPath(item["rel"]).name
            chunks = chunk_transcript(segments, item["rel"], LRC_TITLE, lrc_episode(name), date=lrc_date(name))
            stats["lrc"]["files"] += 1
            stats["lrc"]["chars_before"] += sum(len(s["text"]) for s in segments)
            emit("lrc", chunks)

        # 3. 文件：依組別優先序做段落去重
        deduper = ParagraphDeduper()
        file_details = []
        documents = sorted((i for i in kept if i["group"] not in ("lrc", "asr")), key=keep_order)
        log(f"切 {len(documents)} 個文件（段落去重順序：人紀 → 天紀 → 單篇醫案 → 日誌 → 文章 → 彙編）")
        failed_md5 = {i["md5"] for i in failures}
        for item in documents:
            group = item["group"]
            if item["md5"] in failed_md5:
                errors.append({"path": corpus.safe_path(item["rel"], item["md5"]), "error": "textutil 失敗"})
                continue
            title = corpus.title_for(item["rel"])
            name = PurePosixPath(item["rel"]).name
            try:
                if item["fmt"] == "chm":
                    units = [(f"{item['rel']}#{inner}", paragraphs, find_date(inner))
                             for inner, paragraphs in extractor.chm_pages(item["path"], item["md5"])]
                    units = [(source, [(None, p) for p in paragraphs], date) for source, paragraphs, date in units]
                else:
                    pages = extractor.pages(item)
                    date = None
                    if group in DATED_GROUPS:   # 只有醫案與日誌記日期：檔名優先，其次開頭三段
                        first = next((p for _page, p in pages[:3] if find_date(p)), "")
                        date = find_date(name) or find_date(first)
                    units = [(item["rel"], pages, date)]
            except Exception as error:  # noqa: BLE001 — 單一檔案失敗記進報告，不中斷
                errors.append({"path": corpus.safe_path(item["rel"], item["md5"]),
                               "error": f"{type(error).__name__}: {error}"})
                continue
            stats[group]["files"] += 1
            detail = {"path": corpus.safe_path(item["rel"], item["md5"]), "group": group, "format": item["fmt"],
                      "chars_before": 0, "chars_after": 0, "chunks": 0}
            for source, pages, date in units:
                paragraphs = []
                for page, paragraph in pages:
                    stats[group]["paragraphs_before"] += 1
                    stats[group]["chars_before"] += len(paragraph)
                    detail["chars_before"] += len(paragraph)
                    if corpus.is_boilerplate(paragraph):
                        stats[group]["boilerplate_paragraphs"] += 1
                        continue
                    reason = deduper.check(paragraph)
                    if reason:
                        stats[group][f"dropped_{'paragraph' if reason == '段落相同' else 'sentence'}"] += 1
                        continue
                    stats[group]["paragraphs_after"] += 1
                    stats[group]["chars_after"] += len(paragraph)
                    detail["chars_after"] += len(paragraph)
                    paragraphs.append((page, to_traditional(paragraph)))
                if paragraphs:
                    chunks = chunk_document(paragraphs, source, title, date=date,
                                            track_dates=group in DATED_GROUPS[1:])
                    detail["chunks"] += len(chunks)
                    emit(group, chunks)
            file_details.append(detail)
    os.replace(temp, out_dir / "chunks.jsonl")

    for group in ("asr", "lrc"):
        stats[group]["chars_after"] = stats[group]["chars_before"]
    groups = {}
    for key, (priority, label) in sorted(corpus.GROUPS.items(), key=lambda kv: (kv[1][0], kv[0])):
        if key in stats:
            groups[key] = {"label": label, "dedupe_priority": priority or None, **stats[key]}
    doc_before = sum(v["chars_before"] for k, v in stats.items() if k not in ("asr", "lrc"))
    doc_after = sum(v["chars_after"] for k, v in stats.items() if k not in ("asr", "lrc"))
    report = {
        "created_at": now(),
        "inputs": {"raw_files": len(items), "kept_files": len(kept), "skipped_files": len(skipped),
                   "transcripts": stats["asr"]["files"]},
        "total_chunks": total_chunks,
        "total_chunk_chars": sum(v["chunk_chars"] for v in stats.values()),
        "documents_chars_before_dedupe": doc_before,
        "documents_chars_after_dedupe": doc_after,
        "groups": groups,
        "files": file_details,
        "skipped": sorted(skipped, key=lambda s: s["path"]),
        "errors": errors,
        "warnings": warnings,
        "rar_check": rar_check(items, cache_dir, args.raw_dir, out_dir / "private_rar_unmatched.tsv")
        if not args.skip_rar_check else {"status": "略過（--skip-rar-check）"},
        "elapsed_sec": round(time.time() - started, 1),
    }
    write_atomic(out_dir / "build_report.json", json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    for key, value in groups.items():
        log(f"{value['label']}：{value['files']} 檔，{value['chunks']} 段，"
            f"去重前 {value['chars_before']:,} 字 → 去重後 {value['chars_after']:,} 字")
    log(f"總共 {total_chunks} 段；錯誤 {len(errors)} 個；警告 {len(warnings)} 個；"
        f"報告：{out_dir / 'build_report.json'}")
    return 1 if errors or warnings else 0


# ---------- embed、db ----------

def cmd_embed(args):
    out_dir = Path(args.out_dir)
    chunks_path = out_dir / "chunks.jsonl"
    if not chunks_path.exists():
        log(f"找不到 {chunks_path}")
        return 2
    cache = vertex.EmbedCache(out_dir / "embed_cache.sqlite")
    try:
        if args.dry_run:
            total = cached = estimate = 0
            by_kind = defaultdict(int)
            for line in chunks_path.open(encoding="utf-8"):
                chunk = json.loads(line)
                total += 1
                title = vertex.embed_title(chunk)
                if cache.get(vertex.cache_key(args.model, args.dims, "RETRIEVAL_DOCUMENT", title, chunk["text"])):
                    cached += 1
                else:
                    tokens = vertex.estimate_tokens(title + chunk["text"])
                    estimate += tokens
                    by_kind[chunk["kind"]] += tokens
            log(f"共 {total} 段，已快取 {cached} 段；待送估計 {estimate:,} token，"
                f"約 {estimate / 1e6 * args.price_per_mtok:.2f} 美元（沒有呼叫 API）")
            if by_kind.get("classic"):
                log(f"其中新增經典估計 {by_kind['classic']:,} token（沒有呼叫 API）")
            return 0
        api = vertex.GoogleApi(timeout=args.timeout, log=log)
        client = vertex.EmbeddingClient(api, args.project, args.location, args.model, args.dims)
        meta = vertex.embed_corpus(chunks_path, out_dir, client, cache, batch_size=args.batch_size,
                                   jobs=args.jobs, max_tokens=args.max_tokens,
                                   price_per_mtok=args.price_per_mtok, log=log)
    except vertex.TokenLimitReached as error:
        log(f"停止：{error}")
        return 3
    finally:
        cache.close()
    log(f"完成：{meta['count']} 段，{meta['tokens']:,} token，估計 {meta['estimated_cost_usd']} 美元")
    return 0


def cmd_db(args):
    out_dir = Path(args.out_dir)
    build_db(out_dir / "chunks.jsonl", out_dir / DB_NAME, log=log)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="建索引：ocr、chunks、embed、db")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help=f"產物目錄（預設 {DEFAULT_OUT}）")
        return p

    def gcp(p):
        p.add_argument("--project", default=vertex.DEFAULT_PROJECT)
        p.add_argument("--location", default=vertex.DEFAULT_LOCATION)
        p.add_argument("--bucket", default=vertex.DEFAULT_BUCKET)
        return p

    p = gcp(common(sub.add_parser("ocr", help="Cloud Vision 辨識天機道掃描檔（movie-nas）")))
    p.add_argument("--source", default=TIANJIDAO, help="raw/ 底下的 PDF 相對路徑")
    p.add_argument("--output-prefix", help="Vision 輸出位置（預設 gs://<bucket>/ocr/tianjidao/）")
    p.add_argument("--language-hints", default="zh")
    p.add_argument("--poll-sec", type=int, default=15)
    p.add_argument("--expect-pages", type=int, default=82)
    p.add_argument("--force", action="store_true", help="重新送出並覆蓋本機結果")
    p.set_defaults(func=cmd_ocr)

    p = common(sub.add_parser("chunks", help="轉文字、去重、切段（Mac，不連網）"))
    p.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    p.add_argument("--corrected-dir", type=Path, default=DEFAULT_CORRECTED)
    p.add_argument("--classics-dir", type=Path, help=f"經典解析目錄（預設正式輸出用 {DEFAULT_CLASSICS}）")
    p.add_argument("--ocr-dir", type=Path, help="逐頁文字辨識結果（預設 <out-dir>/ocr）")
    p.add_argument("--jobs", type=int, default=6, help="同時執行的 textutil 數")
    p.add_argument("--skip-rar-check", action="store_true")
    p.set_defaults(func=cmd_chunks)

    p = gcp(common(sub.add_parser("embed", help="Vertex AI 算向量（movie-nas，會花錢）")))
    p.add_argument("--model", default=vertex.MODEL)
    p.add_argument("--dims", type=int, default=vertex.DIMS)
    p.add_argument("--batch-size", type=int, default=1,
                   help=f"一次請求幾筆（官方文件說 gemini-embedding-001 一次一筆；上限 {vertex.MAX_INSTANCES}）")
    p.add_argument("--jobs", type=int, default=4, help="同時送出的請求數")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--max-tokens", type=int, default=15_000_000, help="累計 token 上限（含已快取）")
    p.add_argument("--price-per-mtok", type=float, default=vertex.PRICE_PER_MTOK)
    p.add_argument("--dry-run", action="store_true", help="只估計 token 與費用，不呼叫 API")
    p.set_defaults(func=cmd_embed)

    p = common(sub.add_parser("db", help="建 index.sqlite"))
    p.set_defaults(func=cmd_db)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("已中斷；重跑同一個指令會從快取續跑")
        return 130


if __name__ == "__main__":
    sys.exit(main())
