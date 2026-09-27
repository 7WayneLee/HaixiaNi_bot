#!/usr/bin/env python3
# Claude Code 批次校對的工具閘門（PreToolUse hook）：只放行網路搜尋（WebSearch），
# 每個 session 最多 HAIXIA_CLAUDE_MAX_SEARCH 次（預設 5）；其他工具一律拒絕。
# 拒絕時模型會收到理由並繼續作答。放行時不回覆決定，交給 --allowedTools 判斷。
# Claude 可能在同一則訊息並行搜尋，計數檔要上鎖。每次工具呼叫都記到 audit.jsonl。
import json, os, re, sys, time
from fcntl import LOCK_EX, flock

here = os.path.dirname(os.path.abspath(__file__))
data = json.load(sys.stdin)
name = data.get("tool_name") or ""
session = re.sub(r"[^A-Za-z0-9_.-]", "_", str(data.get("session_id") or "unknown"))[:120]
limit = int(os.environ.get("HAIXIA_CLAUDE_MAX_SEARCH", "5"))
reason = None

if name == "WebSearch":
    counts = os.path.join(here, "counts")
    os.makedirs(counts, exist_ok=True)
    with open(os.path.join(counts, session), "a+") as f:
        flock(f, LOCK_EX)
        f.seek(0)
        used = int(f.read() or 0)
        if used < limit:
            f.seek(0)
            f.truncate()
            f.write(str(used + 1))
        else:
            reason = f"這一段的搜尋次數（{limit} 次）已經用完，請依聲音與上下文判斷，直接輸出校正結果。"
elif name == "WebFetch":
    reason = "批次校對不能開啟網頁全文，請直接用 WebSearch 的搜尋結果判斷。"
else:
    reason = "批次校對只能用 WebSearch 上網搜尋，不能執行指令或讀寫檔案。"

with open(os.path.join(here, "audit.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"t": round(time.time(), 1), "label": os.environ.get("HAIXIA_CLAUDE_LABEL"),
                        "session": session, "tool": name, "input": data.get("tool_input"),
                        "allow": reason is None}, ensure_ascii=False) + "\n")
if reason:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))
