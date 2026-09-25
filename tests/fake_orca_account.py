#!/usr/bin/env python3
"""離線 Orca 額度替身。"""
import json
import os
import sys
import time

if sys.argv[1:] != ["account", "list", "--json"]:
    sys.exit(2)
if os.environ.get("FAKE_ORCA_MISSING"):
    print(json.dumps({"result": {"rateLimits": {"codex": {}}}}))
    sys.exit()
session = float(os.environ.get("FAKE_ORCA_SESSION", "10"))
weekly = float(os.environ.get("FAKE_ORCA_WEEKLY", "10"))
reset_offset = float(os.environ.get("FAKE_ORCA_RESET_OFFSET", "1"))
print(json.dumps({"result": {"rateLimits": {"codex": {
    "session": {"usedPercent": session, "resetsAt": int((time.time() + reset_offset) * 1000)},
    "weekly": {"usedPercent": weekly, "resetsAt": int((time.time() + 86400) * 1000)}
}}}}))
