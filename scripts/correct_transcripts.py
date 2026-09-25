#!/usr/bin/env python3
"""用 Antigravity CLI 並行校正逐字稿，支援斷點續跑。"""

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haixia.correction import (build_prompt, choose_result, corrected_document, evaluate,
                               now, prompt_sha256, split_chunks)
from haixia.transcript import save_corrected, validate, validate_corrected

ROOT = Path(__file__).resolve().parents[1]
QUOTA = re.compile(r"RESOURCE_EXHAUSTED|\b429\b|quota|rate.?limit|too many requests|額度|限速", re.I)
NETWORK = re.compile(r"connection|network|dns|timed? ?out|unreachable|unavailable|socket|ECONN|ENET|連線|網路", re.I)
OUTPUT_LINE = re.compile(r"^\s*\[\d+(?:\.\d+)?\]", re.M)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def setup_hook(work_dir):
    work_dir = Path(work_dir).resolve()
    if work_dir == ROOT or ROOT in work_dir.parents:
        raise ValueError("--work-dir 必須放在 repo 外面")
    ws = work_dir / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    if any(item.name != ".agents" for item in ws.iterdir()):
        raise ValueError("ws 工作目錄只能放 .agents/")
    hooks = ws / ".agents"
    hooks.mkdir(exist_ok=True)
    for name in ("gate.py", "hooks.json"):
        shutil.copyfile(ROOT / "tools/agy_hook" / name, hooks / name)
    return ws


def check_hook(ws, max_search):
    gate = ws / ".agents/gate.py"
    env = dict(os.environ, HAIXIA_AGY_LABEL="自我測試", HAIXIA_AGY_MAX_SEARCH=str(max_search))
    for tool, expected in (("search_web", "allow" if max_search else "deny"), ("run_command", "deny")):
        payload = {"conversationId": f"selftest-{os.getpid()}-{time.time_ns()}", "toolCall": {"name": tool}}
        result = subprocess.run([sys.executable, str(gate)], input=json.dumps(payload),
                                text=True, capture_output=True, cwd=ws / ".agents", env=env, timeout=10)
        if result.returncode or json.loads(result.stdout).get("decision") != expected:
            raise RuntimeError(f"hook 自我測試失敗：{tool}：{result.stderr}")


def check_model(model):
    result = subprocess.run(["agy", "models"], capture_output=True, text=True, timeout=60)
    available = {line.split()[0] for line in result.stdout.splitlines() if line.split()}
    if result.returncode or model not in available:
        raise RuntimeError(f"agy models 看不到指定模型：{model}；{result.stderr.strip()}")


def search_count(audit, label):
    if not audit.exists():
        return 0
    count = 0
    with audit.open(encoding="utf-8") as source:
        for line in source:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("label") == label and item.get("tool") == "search_web" and item.get("allow"):
                count += 1
    return count


def run_agy(prompt, model, timeout, ws, label, max_search, shared):
    args = ["agy", "-p", prompt, "--model", model, "--output-format", "text",
            "--print-timeout", f"{timeout}s", "--disable-slash-commands"]
    env = dict(os.environ, HAIXIA_AGY_LABEL=label, HAIXIA_AGY_MAX_SEARCH=str(max_search))
    began = time.monotonic()
    process = subprocess.Popen(args, cwd=ws, env=env, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    shared.register_process(process)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout + float(os.environ.get("HAIXIA_AGY_TIMEOUT_GRACE_SEC", "30")))
            code = process.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            code = 124
            stderr += "\nAntigravity 呼叫逾時"
    finally:
        shared.unregister_process(process)
    return {"output": stdout, "stderr": stderr, "exit_code": code,
            "elapsed_sec": round(time.monotonic() - began, 2),
            "searches": search_count(ws / ".agents/audit.jsonl", label)}


def error_kind(result):
    if result["exit_code"] == 0 and OUTPUT_LINE.search(result["output"]):
        return None, ""
    stderr = result["stderr"]
    match = re.search(r"AGY_ERROR:\s*(\{[^\n]+\})", stderr)
    detail = stderr
    if match:
        try:
            data = json.loads(match.group(1))
            detail = " ".join(str(value) for value in data.values()) + " " + stderr
        except json.JSONDecodeError:
            pass
    detail = (detail + "\n" + result["output"]).strip()
    if QUOTA.search(detail):
        return "quota", detail[-300:]
    if result["exit_code"] == 0:
        return "empty", ("輸出沒有可辨識的時間標記行：" + detail)[-300:]
    if NETWORK.search(detail):
        return "network", detail[-300:]
    return "other", (detail or "no output produced").strip()[-300:]


class SharedState:
    def __init__(self, work_dir, total, audio_total, files_total=0, backoff_base=300, backoff_max=3600):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.pause_until = 0.0
        self.pause_level = 0
        self.consecutive_errors = 0
        self.processes = set()
        self.force_stop = False
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.work_dir = Path(work_dir)
        self.started = time.monotonic()
        self.counts = {"chunks_total": total, "chunks_done": 0, "chunks_ok": 0,
                       "chunks_partial": 0, "chunks_failed": 0, "files_total": files_total,
                       "files_done": 0,
                       "retries": 0, "searches": 0, "audio_hours_total": audio_total}
        self.audio_hours_done = 0.0
        self.last_error = None
        self.status("running")

    def status(self, state=None):
        with self.lock:
            if state is None:
                state = "paused" if self.pause_until > time.time() else "running"
            data = {"updated_at": now(), "state": state,
                    "paused_until": None if self.pause_until <= time.time() else
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.pause_until)),
                    **self.counts, "audio_hours_done": round(self.audio_hours_done, 4),
                    "consecutive_errors": self.consecutive_errors, "last_error": self.last_error}
            atomic_json(self.work_dir / "status.json", data)

    def wait_if_paused(self):
        while True:
            with self.lock:
                remaining = self.pause_until - time.time()
                if remaining <= 0:
                    if self.pause_until:
                        self.pause_until = 0.0
                        self.status("running")
                    return not self.stop.is_set()
            if self.stop.wait(min(remaining, 1.0)):
                return False

    def note_error(self, kind, reason, log):
        with self.lock:
            self.last_error = reason
            if self.pause_until > time.time():
                self.status("paused")
                return True
            if kind != "quota":
                self.consecutive_errors += 1
            if kind == "quota" or self.consecutive_errors >= 3:
                delay = min(self.backoff_max, self.backoff_base * 2 ** self.pause_level)
                self.pause_level += 1
                self.pause_until = time.time() + delay
                clock = time.strftime("%H:%M", time.localtime(self.pause_until))
                cause = ("Antigravity 額度用完或被限速" if kind == "quota" else
                         f"連續 {self.consecutive_errors} 次呼叫失敗")
                log.warning("暫停：%s（%s），%s 再試", cause, reason, clock)
                self.status("paused")
                return True
            self.status()
            return False

    def note_success(self):
        with self.lock:
            self.consecutive_errors = 0
            self.pause_level = 0

    def register_process(self, process):
        with self.lock:
            self.processes.add(process)
            stopped = self.stop.is_set()
            force = self.force_stop
        if stopped:
            try:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass

    def unregister_process(self, process):
        with self.lock:
            self.processes.discard(process)

    def stop_processes(self):
        """先終止所有進行中的程序群組，五秒後強制清理。"""
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
            time.sleep(0.05)
        with self.lock:
            self.force_stop = True
            processes = list(self.processes)
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def completed(self, result, chunk, audio_hours, log):
        with self.lock:
            self.counts["chunks_done"] += 1
            self.counts[f'chunks_{result["status"]}'] += 1
            self.counts["searches"] += result["searches"]
            self.audio_hours_done += audio_hours
            self.status()
            log.info("%s 第 %d 段：%d 行，%.1f 秒，搜尋 %d 次，%s", result["source"],
                     result["chunk_number"], chunk["end_index"] - chunk["start_index"],
                     result["elapsed_sec"], result["searches"],
                     "失敗" if result["status"] == "failed" else result["status"])

    def progress(self, log):
        with self.lock:
            hours = max((time.monotonic() - self.started) / 3600, 1e-9)
            speed = self.audio_hours_done / hours
            remaining = (self.counts["audio_hours_total"] - self.audio_hours_done) / speed if speed else 0
            average = self.counts["searches"] / self.counts["chunks_done"] if self.counts["chunks_done"] else 0
            log.info("進度：%d/%d 段，音訊 %.2f 小時，速度 %.2f 音訊小時/實際小時，預估剩餘 %.1f 小時；失敗 %d，重試 %d，平均搜尋 %.1f 次",
                     self.counts["chunks_done"], self.counts["chunks_total"], self.audio_hours_done,
                     speed, remaining, self.counts["chunks_failed"], self.counts["retries"], average)


def cache_path(work_dir, source, number):
    ident = hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]
    return Path(work_dir) / "chunks" / ident / f"{number:05d}.json"


def reuse_existing(source, document, number, chunk, chunk_count, existing, args, digest):
    """快取遺失時，從同一版校正版取回已完成的段。"""
    if existing is None or args.force:
        return None
    correction = existing["correction"]
    if (correction["prompt_sha256"] != digest or correction["model"] != args.model or
            correction["max_search"] != args.max_search or
            len(correction["chunks"]) != chunk_count):
        return None
    prior = correction["chunks"][number - 1]
    if prior["status"] != "ok" or prior["start"] != chunk["start"] or prior["end"] != chunk["end"]:
        return None
    start, end = chunk["start_index"], chunk["end_index"]
    old_segments = existing["segments"][start:end]
    new_segments = document["segments"][start:end]
    if len(old_segments) != len(new_segments) or any(
            old["text_asr"] != new["text"] or old["start"] != new["start"] or old["end"] != new["end"]
            for old, new in zip(old_segments, new_segments)):
        return None
    return {"source": source, "chunk_number": number, **prior,
            "lines": [(item["text"], item["corrected"]) for item in old_segments]}


def correct_chunk(job, args, ws, shared, log, digest):
    source, document, number, chunk, existing = job
    if shared.stop.is_set():
        return None
    prompt, labels = build_prompt(document, chunk, args.max_search)
    original = document["segments"][chunk["start_index"]:chunk["end_index"]]
    cache = cache_path(args.work_dir, source, number)
    key = hashlib.sha256((prompt + "\0" + args.model + "\0" + digest).encode("utf-8")).hexdigest()
    if cache.exists() and not args.force:
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved.get("key") == key and not (args.retry_failed and saved["result"]["status"] in {"partial", "failed"}):
                return saved["result"]
        except (ValueError, KeyError):
            pass
    if existing is not None and not args.force:
        return existing
    attempts = []
    elapsed = searches = 0
    failures = 0
    sequence = 0
    previous_attempts = [int(path.stem.rsplit(".attempt", 1)[1])
                         for path in cache.parent.glob(f"{cache.stem}.attempt*.json")
                         if path.stem.rsplit(".attempt", 1)[-1].isdigit()]
    first_attempt_number = max(previous_attempts, default=0)
    while failures <= args.retries and not shared.stop.is_set():
        if not shared.wait_if_paused():
            return None
        sequence += 1
        label = f"{source}#{number}#{time.time_ns()}-{sequence}"
        try:
            response = run_agy(prompt, args.model, args.print_timeout, ws, label, args.max_search, shared)
        except OSError as error:
            response = {"output": "", "stderr": str(error), "exit_code": 127,
                        "elapsed_sec": 0.0, "searches": 0}
        if shared.stop.is_set():
            return None
        response["evaluation"] = evaluate(response["output"], labels, original)
        response["label"] = label
        atomic_json(cache.with_name(f"{cache.stem}.attempt{first_attempt_number + sequence}.json"),
                    {"key": key, **response})
        elapsed += response["elapsed_sec"]
        searches += response["searches"]
        kind, reason = error_kind(response)
        if kind:
            log.warning("%s 第 %d 段第 %d 次呼叫錯誤（%s）：%s", source, number, sequence, kind, reason)
            paused = shared.note_error(kind, reason, log)
            if paused:
                continue
            failures += 1
        else:
            shared.note_success()
            attempts.append(response)
            if response["evaluation"]["valid"]:
                break
            failures += 1
        if failures <= args.retries:
            with shared.lock:
                shared.counts["retries"] += 1
                shared.status()
    if shared.stop.is_set() and failures <= args.retries and not any(
            item["evaluation"]["valid"] for item in attempts):
        return None
    status, lines = choose_result(attempts, original)
    result = {"source": source, "chunk_number": number, "start": chunk["start"],
              "end": chunk["end"], "status": status, "attempts": sequence,
              "searches": searches, "elapsed_sec": elapsed, "lines": lines,
              "fallback_lines": sum(not corrected for _, corrected in lines)}
    atomic_json(cache, {"key": key, "result": result})
    return result


def log_setup(work_dir):
    log = logging.getLogger("haixia.correct")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.StreamHandler(sys.stdout),
                    logging.FileHandler(Path(work_dir) / "logs/correct.log", encoding="utf-8")):
        handler.setFormatter(formatter)
        log.addHandler(handler)
    return log


def collect(args, log):
    selected = []
    if args.files:
        for line in args.files.read_text(encoding="utf-8").splitlines():
            relative = Path(line.strip())
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if relative.is_absolute() or ".." in relative.parts:
                log.error("略過不安全的檔案路徑：%s", line)
                continue
            selected.append(relative)
    else:
        selected = sorted(item.relative_to(args.in_dir) for item in args.in_dir.rglob("*.json"))
    documents = {}
    existing_docs = {}
    failed_files = set()
    total_hours = selected_hours = 0.0
    for relative in selected:
        source = relative.as_posix().removesuffix(".json")
        if args.include and not any(source.startswith(prefix) for prefix in args.include):
            continue
        if not relative.name.endswith(".json"):
            log.error("略過非 JSON 檔：%s", relative)
            continue
        try:
            document = validate(json.loads((args.in_dir / relative).read_text(encoding="utf-8")))
            if document["source"] != source:
                raise ValueError("source 與相對檔名不一致")
        except (OSError, ValueError) as error:
            log.error("壞檔，略過：%s：%s", relative, error)
            continue
        selected_hours += document["duration_sec"] / 3600
        output = args.out_dir / relative
        existing = None
        if output.exists() and not args.force:
            try:
                existing = validate_corrected(json.loads(output.read_text(encoding="utf-8")))
                if existing["source"] != source:
                    raise ValueError("校正版 source 與相對檔名不一致")
                if any(chunk["status"] == "failed" for chunk in existing["correction"]["chunks"]):
                    failed_files.add(source)
            except (OSError, ValueError) as error:
                action = "將重做" if args.retry_failed else "仍跳過；可用 --retry-failed 或 --force 重做"
                log.warning("既有校正版無法驗證，%s：%s：%s", action, relative, error)
                if not args.retry_failed:
                    if args.limit_hours is not None and selected_hours >= args.limit_hours:
                        break
                    continue
            if existing is not None and (not args.retry_failed or all(
                    chunk["status"] == "ok" for chunk in existing["correction"]["chunks"])):
                if args.limit_hours is not None and selected_hours >= args.limit_hours:
                    break
                continue
        documents[source] = (relative, document, split_chunks(document["segments"]))
        if existing is not None:
            existing_docs[source] = existing
        total_hours += document["duration_sec"] / 3600
        if args.limit_hours is not None and selected_hours >= args.limit_hours:
            break
    return documents, total_hours, existing_docs, failed_files


def main(argv=None):
    parser = argparse.ArgumentParser(description="用 Antigravity CLI 批次校正逐字稿")
    for name in ("in-dir", "out-dir", "work-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--model", default="gemini-3.8-flash-high")
    parser.add_argument("--max-search", type=int, default=5)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--print-timeout", type=int, default=900)
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--files", type=Path)
    parser.add_argument("--limit-hours", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.max_search < 0 or args.retries < 0 or args.print_timeout < 1 or (args.limit_hours is not None and args.limit_hours <= 0):
        parser.error("jobs、print-timeout、limit-hours 必須為正數；max-search、retries 不可為負數")
    args.in_dir, args.out_dir, args.work_dir = (item.resolve() for item in (args.in_dir, args.out_dir, args.work_dir))
    (args.work_dir / "logs").mkdir(parents=True, exist_ok=True)
    log = log_setup(args.work_dir)
    ws = setup_hook(args.work_dir)
    documents, hours, existing_docs, failed_files = collect(args, log)
    digest = prompt_sha256()
    jobs = [(source, document, number, chunk,
             reuse_existing(source, document, number, chunk, len(chunks), existing_docs.get(source), args, digest))
            for source, (_, document, chunks) in documents.items()
            for number, chunk in enumerate(chunks, 1)]
    log.info("開始：%d 檔，%.2f 音訊小時，%d 段，jobs=%d，模型=%s",
             len(documents), hours, len(jobs), args.jobs, args.model)
    if args.dry_run:
        for source, document, number, chunk, _existing in jobs:
            prompt, _ = build_prompt(document, chunk, args.max_search)
            path = args.work_dir / "prompts" / hashlib.sha256(source.encode()).hexdigest()[:20] / f"{number:05d}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prompt + "\n", encoding="utf-8")
            log.info("提示詞：%s 第 %d 段 → %s", source, number, path)
        return 0
    empty_sources = [source for source, (_, _, chunks) in documents.items() if not chunks]
    if not jobs:
        shared = SharedState(args.work_dir, 0, hours, len(documents))
        for source in empty_sources:
            relative, original, chunks = documents[source]
            save_corrected(corrected_document(original, chunks, [], args.model,
                                              args.max_search, digest), args.out_dir / relative)
            shared.counts["files_done"] += 1
            shared.audio_hours_done += original["duration_sec"] / 3600
        shared.status("finished")
        log.info("摘要：0 段，輸出 %d 個空白逐字稿", len(empty_sources))
        if failed_files:
            log.error("仍有失敗段落的檔案：%s", "、".join(sorted(failed_files)))
        return 1 if failed_files else 0
    check_model(args.model)
    check_hook(ws, args.max_search)
    base = float(os.environ.get("HAIXIA_BACKOFF_BASE_SEC", "300"))
    maximum = float(os.environ.get("HAIXIA_BACKOFF_MAX_SEC", "3600"))
    shared = SharedState(args.work_dir, len(jobs), hours, len(documents), base, maximum)
    for source in empty_sources:
        relative, original, chunks = documents[source]
        save_corrected(corrected_document(original, chunks, [], args.model,
                                          args.max_search, digest), args.out_dir / relative)
        shared.counts["files_done"] += 1
        shared.audio_hours_done += original["duration_sec"] / 3600
    if empty_sources:
        shared.status()
    by_file = {source: {} for source in documents}
    futures = {}
    interrupted = False
    handled = set()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs)
    def record(future):
        if future.cancelled() or future in handled:
            return
        handled.add(future)
        source, document, number, chunk, _existing = futures[future]
        try:
            result = future.result()
        except Exception as error:
            log.exception("失敗：%s 第 %d 段：%s", source, number, error)
            shared.last_error = str(error)
            status, lines = choose_result([], document["segments"][chunk["start_index"]:chunk["end_index"]])
            result = {"source": source, "chunk_number": number, "start": chunk["start"],
                      "end": chunk["end"], "status": status, "attempts": 0,
                      "searches": 0, "elapsed_sec": 0.0, "lines": lines,
                      "fallback_lines": len(lines)}
        if result is None:
            return
        by_file[source][number] = result
        file_chunks = documents[source][2]
        audio_start = (document["clip"]["start"] if document["clip"] else 0.0) if number == 1 else chunk["start"]
        audio_end = (file_chunks[number]["start"] if number < len(file_chunks) else
                     (document["clip"]["start"] if document["clip"] else 0.0) + document["duration_sec"])
        shared.completed(result, chunk, max(0, audio_end - audio_start) / 3600, log)
        if len(by_file[source]) == len(documents[source][2]):
            relative, original, chunks = documents[source]
            corrected = corrected_document(original, chunks,
                                           [by_file[source][i] for i in range(1, len(chunks) + 1)],
                                           args.model, args.max_search, digest)
            save_corrected(corrected, args.out_dir / relative)
            if any(item["status"] == "failed" for item in by_file[source].values()):
                failed_files.add(source)
            else:
                failed_files.discard(source)
            with shared.lock:
                shared.counts["files_done"] += 1
                shared.status()
    try:
        for job in jobs:
            future = pool.submit(correct_chunk, job, args, ws, shared, log, digest)
            futures[future] = job
        pending = set(futures)
        next_progress = time.monotonic() + 300
        while pending:
            done, pending = concurrent.futures.wait(pending, timeout=min(1, max(0, next_progress - time.monotonic())),
                                                    return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                record(future)
            if time.monotonic() >= next_progress:
                shared.progress(log)
                next_progress = time.monotonic() + 300
    except KeyboardInterrupt:
        interrupted = True
        shared.stop.set()
        for future in futures:
            future.cancel()
        log.warning("收到 Ctrl-C，停止派新段並終止進行中的 Antigravity 呼叫")
        shared.stop_processes()
    finally:
        pool.shutdown(wait=True, cancel_futures=interrupted)
    if interrupted:
        for future in futures:
            if future.done() and not future.cancelled():
                record(future)
    shared.progress(log)
    shared.status("aborted" if interrupted else "finished")
    log.info("摘要：完成 %d/%d 段，ok %d，partial %d，失敗 %d，輸出 %d 檔",
             shared.counts["chunks_done"], shared.counts["chunks_total"],
             shared.counts["chunks_ok"], shared.counts["chunks_partial"],
             shared.counts["chunks_failed"], shared.counts["files_done"])
    if failed_files:
        log.error("仍有失敗段落的檔案：%s", "、".join(sorted(failed_files)))
    return 2 if interrupted else 1 if failed_files else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError) as error:
        sys.exit(f"校正失敗：{error}")
