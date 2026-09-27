#!/usr/bin/env python3
"""離線 Claude Code CLI 替身：模擬 claude -p --output-format stream-json 的事件。
FAKE_CLAUDE_SEARCHES 次 WebSearch 會真的執行 --settings 裡的 PreToolUse hook，被拒就回 is_error 的 tool_result。"""
import json
import os
import re
import subprocess
import sys
import time
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

argv = sys.argv[1:]
if "--bare" in argv or "--dangerously-skip-permissions" in argv or "--" not in argv:
    print("fake claude：不接受 --bare、--dangerously-skip-permissions，提示詞要放在 -- 後面", file=sys.stderr)
    sys.exit(2)
mode = os.environ.get("FAKE_CLAUDE_MODE", "normal")
number = 1
if os.environ.get("FAKE_CLAUDE_COUNTER"):
    with Path(os.environ["FAKE_CLAUDE_COUNTER"]).open("a+") as file:
        flock(file, LOCK_EX)
        file.seek(0)
        number = int(file.read() or "0") + 1
        file.seek(0)
        file.truncate()
        file.write(str(number))
        file.flush()
        flock(file, LOCK_UN)
if os.environ.get("FAKE_CLAUDE_CALL_LOG"):
    with Path(os.environ["FAKE_CLAUDE_CALL_LOG"]).open("a") as file:
        file.write(json.dumps({"number": number, "argv": argv, "cwd": os.getcwd(),
                               "stdin_devnull": os.path.samestat(os.fstat(0), os.stat(os.devnull)),
                               "env": sorted(key for key in os.environ
                                             if key.startswith(("CLAUDE", "ORCA_AGENT", "HAIXIA_CLAUDE")))},
                              ensure_ascii=False) + "\n")


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


reset = int(time.time() + float(os.environ.get("FAKE_CLAUDE_RESET_OFFSET", "3600")))


def rate_limit(status="allowed", kind="five_hour", resets=None):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "resetsAt": resets or reset, "rateLimitType": kind,
        "unifiedWindows": {
            "five_hour": {"utilization": float(os.environ.get("FAKE_CLAUDE_FIVE_HOUR", "0.1")), "resetsAt": reset},
            "seven_day": {"utilization": float(os.environ.get("FAKE_CLAUDE_SEVEN_DAY", "0.3")),
                          "resetsAt": reset + 5 * 86400}}}}


def result(text, error=False, searches=0):
    emit({"type": "result", "subtype": "success", "is_error": error, "result": text, "num_turns": 1,
          "duration_ms": 10, "duration_api_ms": 8, "total_cost_usd": 0.01,
          "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 25,
                    "cache_creation_input_tokens": 10},
          "modelUsage": {"claude-opus-5-5": {"inputTokens": 100, "outputTokens": 50,
                                             "webSearchRequests": searches}}})


emit({"type": "system", "subtype": "init", "tools": ["WebSearch"], "mcp_servers": [], "session_id": "fake"})
if mode == "timeout":
    time.sleep(10)
if mode == "sleep":
    time.sleep(1.5)
if (mode == "usage_limit_once" and number == 1) or (
        mode == "usage_limit" and number <= int(os.environ.get("FAKE_CLAUDE_FAILS", sys.maxsize))):
    # 額度用完：rate_limit_event 為 rejected，assistant 是 <synthetic> 訊息，result 的 is_error 為 true，結束碼 1。
    kind = os.environ.get("FAKE_CLAUDE_LIMIT_KIND", "five_hour")
    rejected_reset = int(time.time() + float(os.environ.get("FAKE_CLAUDE_REJECT_RESET_OFFSET", "-10")))
    emit(rate_limit("rejected", kind, rejected_reset))
    message = "You've hit your limit · resets 2pm (Asia/Taipei)"
    emit({"type": "assistant", "message": {"model": "<synthetic>", "role": "assistant",
                                           "content": [{"type": "text", "text": message}]}})
    result(message, error=True)
    sys.exit(1)

settings = json.loads(argv[argv.index("--settings") + 1])
hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
session = f"fake-{os.getpid()}-{time.time_ns()}"
tools = ["WebSearch"] * int(os.environ.get("FAKE_CLAUDE_SEARCHES", "1"))
if os.environ.get("FAKE_CLAUDE_TOOL"):
    tools.append(os.environ["FAKE_CLAUDE_TOOL"])
allowed = 0
for index, name in enumerate(tools):
    tool_id = f"toolu_{index}"
    emit({"type": "assistant", "message": {"model": "claude-opus-5-5", "role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": {"query": f"查詢 {index}"}}]}})
    payload = {"session_id": session, "hook_event_name": "PreToolUse", "tool_name": name,
               "tool_input": {"query": f"查詢 {index}"}, "tool_use_id": tool_id}
    run = subprocess.run(hook, shell=True, input=json.dumps(payload), text=True, capture_output=True)
    denied = bool(run.stdout.strip()) and json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed += not denied and name == "WebSearch"
    emit({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "is_error": denied,
         "content": "PreToolUse:WebSearch hook error: 拒絕" if denied else "Web search results: quota 429"}]}})
emit(rate_limit())
prompt = argv[-1]
body = prompt.split("## 要校正的行（共 ", 1)[1].split("\n", 1)[1].split("\n## 後文", 1)[0]
lines = [line.replace("麻黄", "麻黃") for line in body.splitlines() if re.match(r"^\[\d+(?:\.\d+)?\]", line)]
if mode == "missing_once" and number == 1:
    lines.pop()
text = "\n".join(lines)
emit({"type": "assistant", "message": {"model": "claude-opus-5-5", "role": "assistant",
                                       "content": [{"type": "text", "text": text}]}})
result(text, searches=allowed)
