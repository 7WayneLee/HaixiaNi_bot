#!/usr/bin/env python3
"""離線 Orca 額度替身。Codex 用 FAKE_ORCA_*，Claude 用 FAKE_ORCA_CLAUDE_*。"""
import json
import os
import sys
import time

if sys.argv[1:] != ["account", "list", "--json"]:
    sys.exit(2)
if os.environ.get("FAKE_ORCA_CALLS"):
    with open(os.environ["FAKE_ORCA_CALLS"], "a") as output:
        output.write("1\n")
if os.environ.get("FAKE_ORCA_MISSING"):
    print(json.dumps({"result": {"rateLimits": {"codex": {}, "claude": {"session": None, "weekly": None,
                                                                         "status": "error"}}}}))
    sys.exit()


def limits(prefix, reset_default="1"):
    session = float(os.environ.get(f"{prefix}SESSION", "10"))
    weekly = float(os.environ.get(f"{prefix}WEEKLY", "10"))
    reset_offset = float(os.environ.get(f"{prefix}RESET_OFFSET", reset_default))
    return {"session": {"usedPercent": session, "resetsAt": int((time.time() + reset_offset) * 1000)},
            "weekly": {"usedPercent": weekly, "resetsAt": int((time.time() + 86400) * 1000)}}


print(json.dumps({"result": {"rateLimits": {"codex": limits("FAKE_ORCA_"),
                                            "claude": limits("FAKE_ORCA_CLAUDE_")}}}))
