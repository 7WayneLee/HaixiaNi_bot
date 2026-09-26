#!/usr/bin/env python3
"""測試用 Antigravity 替身；不連網。"""
import atexit
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from fcntl import LOCK_EX, LOCK_UN, flock


def bump(path):
    """跨程序遞增計數檔，回傳新值。"""
    with Path(path).open("a+") as output:
        flock(output, LOCK_EX)
        output.seek(0)
        value = int(output.read() or "0") + 1
        output.seek(0)
        output.truncate()
        output.write(str(value))
        output.flush()
        flock(output, LOCK_UN)
    return value


def current_account():
    """模擬鑰匙圈裡的登入帳號：FAKE_AGY_ACCOUNT_FILE 優先（測試可中途改寫），其次 FAKE_AGY_ACCOUNT；空字串表示 log 裡沒有帳號。"""
    path = os.environ.get("FAKE_AGY_ACCOUNT_FILE")
    if path and Path(path).exists():
        return Path(path).read_text().strip()
    return os.environ.get("FAKE_AGY_ACCOUNT", "a@example.com")


def write_log(account):
    """像真的 agy 一樣把 applyAuthResult 寫進 --log-file（根層級旗標）。"""
    if not log_file:
        return
    with Path(log_file).open("a") as output:
        output.write("I0926 17:42:37.562615     264 keyring.go:64] keyringAuth: loaded token, expired=false\n")
        if account:
            output.write(f"I0926 17:42:37.562708       1 server_oauth.go:196] applyAuthResult: "
                         f"email={account}, authMethod=consumer, quotaProject=\n")


argv = sys.argv[1:]
log_file = None
if argv[:1] == ["--log-file"]:
    log_file, argv = argv[1], argv[2:]
if argv[:1] == ["models"]:
    if argv[1:]:
        # 真的 agy：models 子命令不認得 --log-file 等旗標。
        print("Error: flags provided but not defined: " + argv[1], file=sys.stderr)
        sys.exit(2)
    if os.environ.get("FAKE_AGY_MODELS_COUNTER"):
        bump(os.environ["FAKE_AGY_MODELS_COUNTER"])
    write_log(current_account())
    print("gemini-3.8-flash-high\tGemini 3.8 Flash (High)")
    sys.exit(0)

mode = os.environ.get("FAKE_AGY_MODE", "normal")
flag = os.environ.get("FAKE_AGY_FLAG")
started = os.environ.get("FAKE_AGY_STARTED")
if mode == "hang_ignore_term":
    signal.signal(signal.SIGTERM, lambda *_: None)
if started:
    Path(started).write_text(str(os.getpid()))
label = os.environ.get("HAIXIA_AGY_LABEL", "")
began = time.time()
outcome = "ok"
call_log = os.environ.get("FAKE_AGY_CALL_LOG")
if call_log:
    with Path(call_log).open("a") as output:
        output.write(label + "\n")
number = 0
counter = os.environ.get("FAKE_AGY_COUNTER")
if counter:
    number = bump(counter)
# 模擬另一個 agy 視窗更新登入資料：第 FAKE_AGY_FLIP_AT 次呼叫開始時，鑰匙圈的帳號被換成 FAKE_AGY_FLIP_TO。
flip_at = os.environ.get("FAKE_AGY_FLIP_AT")
if flip_at and number == int(flip_at):
    Path(os.environ["FAKE_AGY_ACCOUNT_FILE"]).write_text(os.environ["FAKE_AGY_FLIP_TO"] + "\n")
write_log(current_account())
timeline = os.environ.get("FAKE_AGY_TIMELINE")
if timeline:
    def write_timeline():
        with Path(timeline).open("a") as output:
            output.write(f"{number} {began:.6f} {time.time():.6f} {outcome}\n")
    atexit.register(write_timeline)
if mode == "account":
    # 模擬帳號額度：前 FAKE_AGY_QUOTA_CALLS 次，或 FAKE_AGY_SWITCH 檔不存在（還沒換帳號）時回 429。
    switch = os.environ.get("FAKE_AGY_SWITCH")
    limit = int(os.environ.get("FAKE_AGY_QUOTA_CALLS", "0"))
    if (limit and number <= limit) or (switch and not Path(switch).exists()):
        outcome = "quota"
        time.sleep(0.2)
        resets = os.environ.get("FAKE_AGY_RESETS", "2h31m45s")
        short = "RESOURCE_EXHAUSTED (code 429): Individual quota reached." + (f" Resets in {resets}" if resets else "")
        print("AGY_ERROR: " + json.dumps({"short_error": short, "status": 429}), file=sys.stderr)
        sys.exit(3)
    time.sleep(float(os.environ.get("FAKE_AGY_SLEEP", "0")))
if mode == "quota_once" and flag and not Path(flag).exists():
    Path(flag).write_text("1")
    print('AGY_ERROR: {"retryable": true, "status": 429, "code": "RESOURCE_EXHAUSTED"}', file=sys.stderr)
    sys.exit(3)
if mode == "quota" or (mode == "quota_first_four_then_normal" and number <= 4):
    if mode == "quota_first_four_then_normal":
        time.sleep(0.15)
    print('AGY_ERROR: {"retryable": true, "status": 429, "code": "RESOURCE_EXHAUSTED"}', file=sys.stderr)
    sys.exit(3)
if mode == "quota_stdout_once" and number == 1:
    print("RESOURCE_EXHAUSTED：今日額度用完")
    sys.exit(0)
if mode in {"empty", "empty_once"} and (mode == "empty" or number == 1):
    print("沒有可校正內容")
    sys.exit(0)
if mode == "error_three_then_normal" and number <= 3:
    print('AGY_ERROR: {"retryable": false, "code": "BROKEN_CLIENT"}', file=sys.stderr)
    sys.exit(3)
if mode == "timeout":
    time.sleep(10)
if mode == "sleep":
    time.sleep(1.5)
if mode == "hang_ignore_term":
    time.sleep(30)
if flag:
    with Path(flag).open("a") as output:
        output.write("x")
prompt = sys.argv[sys.argv.index("-p") + 1]
body = prompt.split("## 要校正的行（共 ", 1)[1].split("\n", 1)[1].split("\n## 後文", 1)[0]
lines = [line for line in body.splitlines() if re.match(r"^\[\d+(?:\.\d+)?\]", line)]
if mode == "missing":
    lines = lines[:-1]
if mode == "missing_once" and number == 1:
    lines = lines[:-1]
bad_chunk = os.environ.get("FAKE_AGY_BAD_CHUNK")
if bad_chunk and f"#{bad_chunk}#" in label:
    lines = lines[:-1]
for index, line in enumerate(lines):
    if mode == "short":
        print(line.split("]", 1)[0] + "] 短")
    elif index == 0 and mode == "long_one":
        print(line.split("]", 1)[0] + "] " + "長" * 30)
    elif index == 0 and mode == "blank_one":
        print(line.split("]", 1)[0] + "] ")
    else:
        print(line.replace("麻黄", "麻黃"))
