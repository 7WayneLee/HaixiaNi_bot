#!/usr/bin/env python3
"""離線 Codex exec 替身。"""
import json
import os
import re
import signal
import sys
import time
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

mode = os.environ.get("FAKE_CODEX_MODE", "normal")
counter = os.environ.get("FAKE_CODEX_COUNTER")
number = 1
if counter:
    with Path(counter).open("a+") as file:
        flock(file, LOCK_EX)
        file.seek(0)
        number = int(file.read() or "0") + 1
        file.seek(0)
        file.truncate()
        file.write(str(number))
        file.flush()
        flock(file, LOCK_UN)
if os.environ.get("FAKE_CODEX_CALL_LOG"):
    with Path(os.environ["FAKE_CODEX_CALL_LOG"]).open("a") as file:
        file.write(f"{number}\n")
if mode == "timeout":
    time.sleep(10)
if mode == "sleep":
    time.sleep(1.5)
if mode == "disconnect_once" and number == 1:
    print("stream disconnected before completion: websocket closed by server", file=sys.stderr)
    print("unexpected status 401 Unauthorized: Incorrect API key provided: sk-ABC*123", file=sys.stderr)
    print(json.dumps({"type": "turn.failed"}))
    sys.exit(1)
if mode == "usage_limit_once" and number == 1:
    print("Usage limit reached. You've reached your usage limit.", file=sys.stderr)
    print(json.dumps({"type": "turn.failed"}))
    sys.exit(1)
prompt = sys.argv[-1]
body = prompt.split("## 要校正的行（共 ", 1)[1].split("\n", 1)[1].split("\n## 後文", 1)[0]
lines = [line.replace("麻黄", "麻黃") for line in body.splitlines()
         if re.match(r"^\[\d+(?:\.\d+)?\]", line)]
if mode == "missing_once" and number == 1:
    lines.pop()
Path(sys.argv[sys.argv.index("-o") + 1]).write_text("\n".join(lines))
print(json.dumps({"type": "item.completed", "item": {"type": "web_search"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100,
                   "cached_input_tokens": 25, "output_tokens": 50,
                   "reasoning_output_tokens": 10}}))
