#!/usr/bin/env python3
"""只讀監視校正批次；需要時透過 Orca 通知指揮。"""

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path


def local_now():
    return datetime.now().astimezone()


def parse_time(value, tz):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)
    except (TypeError, ValueError):
        return None


LONG_RESET = re.compile(r"Resets\s+in\s+((?:\d+[dhms])+)", re.I)


def long_reset_hours(text):
    """額度錯誤的重設倒數超過 6 小時（通常是週額度）時回傳小時數，否則回傳 None。"""
    longest = 0
    for match in LONG_RESET.finditer(text or ""):
        units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        seconds = sum(int(number) * units[unit.lower()]
                      for number, unit in re.findall(r"(\d+)([dhms])", match.group(1), re.I))
        longest = max(longest, seconds)
    return longest / 3600 if longest > 6 * 3600 else None


def pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class CorrectionWatcher:
    def __init__(self, work_dir, interval_min=10, notify_run=None, orca_bin="orca",
                 min_free_gb=5, daily_status_hour=9, now_fn=local_now,
                 pid_alive_fn=pid_alive, free_bytes_fn=None):
        self.work_dir = Path(work_dir)
        self.interval_min = interval_min
        self.notify_run = notify_run
        self.orca_bin = orca_bin
        self.min_free_gb = min_free_gb
        self.daily_status_hour = daily_status_hour
        self.now_fn = now_fn
        self.pid_alive_fn = pid_alive_fn
        self.free_bytes_fn = free_bytes_fn or (lambda path: shutil.disk_usage(path).free)
        self.watch_log = self.work_dir / "logs/watch.log"
        self.correct_log = self.work_dir / "logs/correct.log"
        self.log_identity = None
        self.log_offset = 0
        self.log_fragment = ""
        self.active_alerts = set()
        self.last_alert = {}
        self.failed_baseline = 0
        self.previous_failed = None
        self.last_quota_pause = None
        self.last_daily_date = None
        self.engine_stops_seen = set()

    def write(self, level, now, kind, description):
        line = f"{level} {now:%Y-%m-%d %H:%M:%S} {kind}：{description}"
        print(line, flush=True)
        self.watch_log.parent.mkdir(parents=True, exist_ok=True)
        with self.watch_log.open("a", encoding="utf-8") as output:
            output.write(line + "\n")

    def notify(self, kind, subject, body, now):
        if not self.notify_run:
            return
        command = [self.orca_bin, "orchestration", "send", "--to", f"run:{self.notify_run}",
                   "--type", kind, "--subject", subject, "--body", body, "--json"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode:
                self.write("INFO", now, "通知失敗", (result.stderr or result.stdout).strip())
        except (OSError, subprocess.TimeoutExpired) as error:
            self.write("INFO", now, "通知失敗", str(error))

    def alert(self, kind, description, now, present=True):
        if not present:
            self.active_alerts.discard(kind)
            return False
        previous = self.last_alert.get(kind)
        if kind in self.active_alerts and previous and now - previous < timedelta(hours=6):
            return False
        self.write("ALERT", now, kind, description)
        self.notify("escalation", f"校正監視：{kind}", description, now)
        self.active_alerts.add(kind)
        self.last_alert[kind] = now
        return True

    def new_log_lines(self):
        try:
            stat = self.correct_log.stat()
        except FileNotFoundError:
            if self.log_identity is None:
                self.log_identity = (None, None)
            return []
        identity = (stat.st_dev, stat.st_ino)
        if self.log_identity is None:
            self.log_identity = identity
            self.log_offset = stat.st_size
            return []
        if identity != self.log_identity or stat.st_size < self.log_offset:
            self.log_identity = identity
            self.log_offset = 0
            self.log_fragment = ""
        with self.correct_log.open("rb") as source:
            source.seek(self.log_offset)
            added = source.read()
            self.log_offset = source.tell()
        pieces = (self.log_fragment + added.decode("utf-8", errors="replace")).split("\n")
        self.log_fragment = pieces.pop()
        return pieces

    def failed_filenames(self, status):
        if not status.get("chunks_failed", 0):
            return []
        if isinstance(status.get("failed_files"), list):
            return [str(name) for name in status["failed_files"]]
        try:
            log = self.correct_log.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        lines = re.findall(r"仍有失敗段落的檔案：([^\n]+)", log)
        return lines[-1].strip().split("、") if lines else []

    def progress_summary(self, status):
        return (f'完成 {status.get("chunks_done", 0)}/{status.get("chunks_total", 0)} 段，'
                f'ok {status.get("chunks_ok", 0)}、partial {status.get("chunks_partial", 0)}、'
                f'failed {status.get("chunks_failed", 0)}；'
                f'音訊 {status.get("audio_hours_done", 0)}/{status.get("audio_hours_total", 0)} 小時')

    def check_once(self):
        now = self.now_fn()
        lines = self.new_log_lines()
        try:
            status = json.loads((self.work_dir / "status.json").read_text(encoding="utf-8"))
            if not isinstance(status, dict):
                raise ValueError("狀態不是 JSON 物件")
        except (OSError, ValueError) as error:
            self.alert("狀態讀取失敗", str(error), now)
            return False
        self.alert("狀態讀取失敗", "", now, False)
        for engine, detail in status.get("engines", {}).items():
            if detail.get("state") != "stopped":
                continue
            reason = str(detail.get("reason") or "")
            key = (engine, reason)
            if key in self.engine_stops_seen:
                continue
            self.engine_stops_seen.add(key)
            if engine == "codex" and "週額度" in reason:
                self.write("INFO", now, "Codex 週額度停止", reason)
            elif reason != "執行結束":
                self.alert(f"{engine} 引擎停止", reason, now)
        state = status.get("state")
        if state == "finished":
            names = self.failed_filenames(status)
            body = self.progress_summary(status) + "；仍有 failed 段的檔案：" + ("、".join(names) if names else "無")
            self.write("STATUS", now, "校正執行結束", body)
            self.notify("status", "校正執行結束", body, now)
            return True
        if state == "aborted":
            body = self.progress_summary(status)
            self.write("STATUS", now, "校正已手動中止", body)
            self.notify("status", "校正已手動中止", body, now)
            return True

        self.alert("程式意外結束", f'PID {status.get("pid")} 已不存在，state={state}', now,
                   not self.pid_alive_fn(status.get("pid")))
        updated = parse_time(status.get("updated_at"), now.tzinfo)
        progress = parse_time(status.get("last_progress_at") or status.get("started_at"), now.tzinfo)
        stale = (state == "running" and updated is not None and progress is not None
                 and now - updated > timedelta(minutes=45) and now - progress > timedelta(minutes=45))
        self.alert("卡住", "running 狀態超過 45 分鐘沒有狀態更新或完成段落", now, stale)
        paused_until = parse_time(status.get("paused_until"), now.tzinfo)
        overdue = (state == "paused" and paused_until is not None
                   and now - paused_until > timedelta(minutes=30))
        self.alert("暫停逾時", f'原定 {status.get("paused_until")} 再試，已逾時超過 30 分鐘', now, overdue)

        failed = int(status.get("chunks_failed", 0))
        if self.previous_failed is not None and failed <= self.previous_failed:
            self.active_alerts.discard("新的失敗段")
            self.failed_baseline = failed
        failure_event = failed - self.failed_baseline >= 3
        if failure_event and self.alert("新的失敗段", f'failed 段新增 {failed - self.failed_baseline} 段，累計 {failed} 段', now):
            self.failed_baseline = failed
        self.previous_failed = failed

        breaker = [line for line in lines if re.search(r"連續\s*\d+\s*次呼叫失敗", line)]
        self.alert("斷路器暫停", breaker[-1] if breaker else "", now, bool(breaker))
        try:
            free = self.free_bytes_fn(self.work_dir)
            self.alert("磁碟空間不足", f'剩餘 {free / 1024**3:.2f} GiB，低於 {self.min_free_gb:g} GiB',
                       now, free < self.min_free_gb * 1024**3)
            self.alert("磁碟檢查失敗", "", now, False)
        except OSError as error:
            self.alert("磁碟檢查失敗", str(error), now)

        quota = status.get("quota_resets_at") if state == "paused" else None
        quota_lines = [line for line in lines if "暫停：Antigravity 額度用完" in line]
        quota_key = quota or (quota_lines[-1] if quota_lines else None)
        if quota_key and quota_key != self.last_quota_pause:
            self.write("INFO", now, "額度暫停", f'預計 {quota or status.get("paused_until")} 重試')
            self.last_quota_pause = quota_key
        if state != "paused":
            self.last_quota_pause = None

        # 週額度用完時請使用者換 Gemini 帳號；同一類通知 6 小時內只送一次。
        agy = (status.get("engines") or {}).get("agy") or {}
        agy_paused = agy.get("state") == "paused" if agy else state == "paused"
        weekly = long_reset_hours(str(agy.get("reason") or status.get("last_error") or "")) if agy_paused else None
        kind = "Antigravity 週額度用完"
        previous = self.last_alert.get(kind)
        if weekly is not None and (previous is None or now - previous >= timedelta(hours=6)):
            self.active_alerts.discard(kind)
            self.alert(kind, f"錯誤訊息顯示約 {weekly:.0f} 小時後才重設，請切換到另一個 Gemini 帳號；"
                             "換好後校正程式最慢一小時內會自動接上", now)

        if (self.daily_status_hour >= 0 and now.hour >= self.daily_status_hour
                and self.last_daily_date != now.date()):
            body = self.progress_summary(status) + f'；狀態 {state}'
            self.write("STATUS", now, "校正每日進度", body)
            self.notify("status", "校正每日進度", body, now)
            self.last_daily_date = now.date()
        return False

    def run(self):
        while not self.check_once():
            time.sleep(self.interval_min * 60)


def main(argv=None):
    parser = argparse.ArgumentParser(description="只讀監視校正作業，不呼叫 AI")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--interval-min", type=float, default=10)
    parser.add_argument("--notify-run")
    parser.add_argument("--orca-bin", default="orca")
    parser.add_argument("--min-free-gb", type=float, default=5)
    parser.add_argument("--daily-status-hour", type=int, default=9)
    args = parser.parse_args(argv)
    if args.interval_min <= 0 or args.min_free_gb < 0 or not -1 <= args.daily_status_hour <= 23:
        parser.error("interval-min 必須大於 0、min-free-gb 不可為負，daily-status-hour 須為 -1 到 23")
    CorrectionWatcher(args.work_dir, args.interval_min, args.notify_run, args.orca_bin,
                      args.min_free_gb, args.daily_status_hour).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
