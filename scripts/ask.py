#!/usr/bin/env python3
"""用 Claude 或 Vertex AI Gemini 問倪師資料（Telegram bot 用同一個問答核心）。

    scripts/ask.py "桂枝湯和麻黃湯怎麼分？"
    scripts/ask.py -i                     # 多輪互動：/new 開新對話，/quit 或 Ctrl-D 結束
    scripts/ask.py --model claude-sonnet-5 --effort low --show-tools "少陽病的提綱"

金鑰：環境變數 ANTHROPIC_API_KEY，沒有才讀 repo 根目錄的 .env；組織層級的金鑰另外設定
ANTHROPIC_WORKSPACE_ID。查詢向量走 Vertex AI（movie-nas 的預設服務帳號），失敗時自動只用關鍵字搜尋。
每題的用量與估計費用寫進 ~/haixia-bot-logs/answers.jsonl。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haixia import answer as core

DEFAULT_INDEX = Path.home() / "haixia-index-build"
TWD_PER_USD = 32


def print_tool(record):
    if record["name"] == "search":
        args = record["input"]
        if isinstance(args, dict) and "query" in args:
            scope = core.SCOPE_NAMES.get(args.get("kind", "any"), args.get("kind"))
            title = f"搜尋「{args['query']}」（{scope}，{args.get('k', core.DEFAULT_K)} 段）"
        else:
            title = f"搜尋 {args!r}"
        if record["mode"] == "bm25":
            title += "［只用關鍵字］"
    else:
        args = record["input"]
        title = f"讀前後文 {args.get('id') if isinstance(args, dict) else args!r}"
    print(f"  ［第 {record['round']} 輪］{title}", file=sys.stderr)
    if record["error"]:
        print(f"    錯誤：{record['error']}", file=sys.stderr)
    for hit in record["hits"]:
        print(f"    - {hit['citation']}（{hit['id']}）", file=sys.stderr)


def usage_line(usage, cost, rounds=None, requests=None, elapsed=None):
    parts = [f"輸入 {usage['input_tokens']:,}（快取讀 {usage['cache_read_input_tokens']:,}、"
             f"快取寫 {usage['cache_creation_input_tokens']:,}）、輸出 {usage['output_tokens']:,} token"]
    if rounds is not None:
        parts.append(f"工具 {rounds} 輪、請求 {requests} 次")
    parts.append(f"估計 US${cost:.4f}（約 NT${cost * TWD_PER_USD:.2f}）")
    if elapsed is not None:
        parts.append(f"{elapsed:.1f} 秒")
    return "；".join(parts)


def show(result, conversation=None, show_thinking=False):
    if show_thinking:
        print("［思考過程］", file=sys.stderr)
        print(result.thinking or "（這題沒有思考內容）", file=sys.stderr)
    print(core.display_answer(result.text, conversation) if conversation is not None else result.text)
    print(file=sys.stderr)
    for note in result.notes:
        print(f"［注意］{note}", file=sys.stderr)
    if result.fallback:
        print(f"［注意］這題改由 {result.model} 回答（伺服器端 fallback）", file=sys.stderr)
    print("［用量］" + usage_line(result.usage, result.cost_usd, result.rounds, result.requests,
                                 result.elapsed_sec), file=sys.stderr)


def interactive(ask, show_thinking=False):
    conversation = core.Conversation()
    total = dict.fromkeys(core.USAGE_FIELDS, 0)
    cost = 0.0
    print("多輪問答：/new 開新對話，/quit 或 Ctrl-D 結束。", file=sys.stderr)
    while True:
        try:
            question = input("\n問題> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break
        if not question:
            continue
        if question in ("/quit", "/exit"):
            break
        if question == "/new":
            conversation = core.Conversation()
            print("（已開新對話）", file=sys.stderr)
            continue
        result = ask(conversation, question)
        if result is None:
            continue
        show(result, conversation, show_thinking)
        for name in core.USAGE_FIELDS:
            total[name] += result.usage[name]
        cost += result.cost_usd
    print("［本次合計］" + usage_line(total, cost), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description="用 Claude 或 Gemini 問倪師資料")
    parser.add_argument("question", nargs="?", help="問題（用 -i 時可省略）")
    parser.add_argument("-i", "--interactive", action="store_true", help="多輪互動")
    parser.add_argument("--model", default=core.DEFAULT_MODEL,
                        help=f"模型（預設 {core.DEFAULT_MODEL}；可用 {core.COMPARE_MODEL}、gemini-3.8-flash、gemini-3.1-pro-preview）")
    parser.add_argument("--effort", choices=core.EFFORTS, default=core.DEFAULT_EFFORT,
                        help=f"Claude 思考深度（預設 {core.DEFAULT_EFFORT}）")
    parser.add_argument("--gemini-thinking", choices=("default", "low", "medium", "high"),
                        default="default", help="Gemini 思考程度；default 用模型預設")
    parser.add_argument("--show-tools", action="store_true", help="印出每次搜尋的查詢與命中出處")
    parser.add_argument("--show-thinking", action="store_true", help="在答案前把官方思考摘要印到 stderr")
    parser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--bm25-only", action="store_true", help="不呼叫 Vertex，只用關鍵字搜尋")
    parser.add_argument("--max-tool-rounds", type=int, default=core.MAX_TOOL_ROUNDS)
    parser.add_argument("--fallback", choices=["auto", "on", "off"], default="auto",
                        help="伺服器端 fallback（auto：Opus 5.5 等模型開）")
    parser.add_argument("--prices", type=Path, help="JSON 價格表，覆蓋預設價格")
    parser.add_argument("--log-dir", type=Path, default=core.DEFAULT_LOG_DIR)
    parser.add_argument("--no-log", action="store_true", help="不寫 JSONL 紀錄")
    args = parser.parse_args(argv)
    if not args.interactive and not args.question:
        parser.error("請給問題，或用 -i 進入互動模式")

    gemini = args.model.startswith("gemini-")
    api_key = workspace_id = None
    if not gemini:
        try:
            api_key = core.resolve_api_key()
        except core.MissingApiKey as problem:
            print(problem, file=sys.stderr)
            return 2
        workspace_id = core.resolve_workspace_id()
    try:
        prices = core.load_prices(args.prices) if args.prices else None
        if gemini:
            from haixia import answer_gemini
            answerer = answer_gemini.Answerer(
                args.index_dir, args.model, thinking_level=args.gemini_thinking,
                embedder=None if args.bm25_only else "vertex", max_tool_rounds=args.max_tool_rounds,
                log_dir=None if args.no_log else args.log_dir, prices=prices)
        else:
            answerer = core.Answerer(
                args.index_dir, args.model, args.effort, api_key=api_key, workspace_id=workspace_id,
                embedder=None if args.bm25_only else "vertex", max_tool_rounds=args.max_tool_rounds,
                fallback={"auto": None, "on": True, "off": False}[args.fallback],
                log_dir=None if args.no_log else args.log_dir, prices=prices)
    except (ValueError, OSError) as problem:
        print(core.redact(problem, api_key, workspace_id), file=sys.stderr)
        return 2

    def ask(conversation, question):
        try:
            return answerer.ask(conversation, question, on_tool=print_tool if args.show_tools else None)
        except Exception as problem:  # noqa: BLE001 — 避免 SDK 錯誤內容洩漏憑證
            status = getattr(problem, "status_code", None) or getattr(problem, "code", None)
            message = f"回答服務暫時失敗（{type(problem).__name__}，HTTP {status or '未知'}），請稍後再試"
        print(core.redact(message, api_key, workspace_id), file=sys.stderr)
        return None

    try:
        if args.interactive:
            interactive(ask, args.show_thinking)
            return 0
        conversation = core.Conversation()
        result = ask(conversation, args.question)
        if result is None:
            return 1
        show(result, conversation, args.show_thinking)
        return 0
    finally:
        answerer.close()


if __name__ == "__main__":
    sys.exit(main())
