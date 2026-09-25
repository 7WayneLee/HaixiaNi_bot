#!/usr/bin/env python3
# 批次校對的工具閘門：只放行網路搜尋（search_web），每次對話最多 HAIXIA_AGY_MAX_SEARCH 次（預設 5）；
# 開網頁、執行指令、讀寫檔案一律拒絕。用 hook 拒絕，模型會收到理由並繼續作答
# （agy 非互動模式自己的權限拒絕會讓整次呼叫沒有輸出）。每次工具呼叫都記到 audit.jsonl。
import json, os, sys, time

here = os.path.dirname(os.path.abspath(__file__))
data = json.load(sys.stdin)
call = data.get("toolCall") or {}
name = call.get("name", "")
conv = data.get("conversationId") or "unknown"
limit = int(os.environ.get("HAIXIA_AGY_MAX_SEARCH", "5"))

if name == "search_web":
    counts = os.path.join(here, "counts")
    os.makedirs(counts, exist_ok=True)
    path = os.path.join(counts, conv)
    used = int(open(path).read() or 0) if os.path.exists(path) else 0
    if used < limit:
        with open(path, "w") as f:
            f.write(str(used + 1))
        result = {"decision": "allow"}
    else:
        result = {"decision": "deny", "reason": f"這一段的搜尋次數（{limit} 次）已經用完，請依聲音與上下文判斷，直接輸出校正結果。"}
elif name.startswith("read_url") or name == "view_content_chunk":
    result = {"decision": "deny", "reason": "批次校對不能開啟網頁全文，請直接用 search_web 的搜尋結果判斷。"}
else:
    result = {"decision": "deny", "reason": "批次校對只能用 search_web 上網搜尋，不能執行指令或讀寫檔案。"}

with open(os.path.join(here, "audit.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"t": round(time.time(), 1), "label": os.environ.get("HAIXIA_AGY_LABEL"), "conv": conv,
                        "tool": name, "args": call.get("args"), "allow": result["decision"] == "allow"},
                       ensure_ascii=False) + "\n")
print(json.dumps(result, ensure_ascii=False))
