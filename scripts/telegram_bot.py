#!/usr/bin/env python3
"""倪海廈教學研讀助手的 Telegram bot（第五步；long polling，不需要網域或 HTTPS）。

    .venv-index/bin/python scripts/telegram_bot.py            # 前景執行（平常由 systemd 的 haixia-bot 服務執行）
    .venv-index/bin/python scripts/telegram_bot.py --check    # 只檢查設定與索引，不連 Telegram

設定（環境變數優先，沒有才讀 repo 根目錄的 .env）：TELEGRAM_BOT_TOKEN、TELEGRAM_ALLOWED_USER_IDS（逗號分隔）、
ANTHROPIC_API_KEY（組織層級的金鑰另加 ANTHROPIC_WORKSPACE_ID）、DAILY_BUDGET_USD（預設 3）。
token 與金鑰不會印出來；log 裡出現時換成 ***。說明見 docs/06-telegram.md。
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haixia import answer as core
from haixia import telegram_bot as bot
from haixia.search import Searcher

DEFAULT_INDEX = Path.home() / "haixia-index-build"


def setup_logging(*secrets):
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s：%(message)s"))
    handler.addFilter(bot.RedactFilter(*secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # httpx 在 INFO 會印出完整網址，Telegram 的網址裡有 bot token
    for name in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv=None):
    parser = argparse.ArgumentParser(description="倪海廈教學研讀助手的 Telegram bot")
    parser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--log-dir", type=Path, default=core.DEFAULT_LOG_DIR,
                        help="問答紀錄 JSONL 的資料夾（每日花費也從這裡加總）")
    parser.add_argument("--effort", choices=core.EFFORTS, default=core.DEFAULT_EFFORT)
    parser.add_argument("--gemini-thinking", choices=("default", "low", "medium", "high"),
                        default="default", help="Gemini 思考程度；default 用模型預設")
    parser.add_argument("--bm25-only", action="store_true", help="不呼叫 Vertex，只用關鍵字搜尋")
    parser.add_argument("--check", action="store_true", help="只檢查設定與索引，不連 Telegram、不呼叫 API")
    args = parser.parse_args(argv)

    try:
        config = bot.load_config()
    except bot.ConfigError as problem:
        print(f"設定有誤：{problem}", file=sys.stderr)
        return 2
    setup_logging(config.token, config.api_key, config.workspace_id)
    log = logging.getLogger("haixia.bot")

    def make_searcher():
        embedder = None if args.bm25_only else core.default_embedder()
        return Searcher(args.index_dir, embedder)

    def make_answerer(model, searcher):
        if model.startswith("gemini-"):
            from haixia import answer_gemini
            return answer_gemini.Answerer(args.index_dir, model, searcher=searcher,
                                          thinking_level=args.gemini_thinking, log_dir=args.log_dir)
        return core.Answerer(args.index_dir, model, args.effort, api_key=config.api_key,
                             workspace_id=config.workspace_id, searcher=searcher, log_dir=args.log_dir)

    if args.check:
        try:
            searcher = make_searcher()
        except Exception as problem:  # noqa: BLE001
            print(f"索引打不開（{args.index_dir}）：{type(problem).__name__}", file=sys.stderr)
            return 2
        vectors = "有" if searcher.store.vectors is not None else f"沒有（{searcher.store.vector_problem}）"
        print(f"設定正確：允許 {len(config.allowed)} 個使用者，每日上限 US${config.daily_budget:.2f}；"
              f"索引 {searcher.store.count} 段，向量{vectors}。")
        searcher.close()
        return 0

    pool = bot.AnswererPool(make_searcher, make_answerer)
    bot_core = bot.BotCore(config.allowed, pool, daily_budget=config.daily_budget, log_dir=args.log_dir)
    application = bot.build_application(bot_core, config.token)
    log.info("bot 啟動：允許 %d 個使用者，每日上限 US$%.2f，預設模型 %s，effort %s%s",
             len(config.allowed), config.daily_budget, core.DEFAULT_MODEL, args.effort,
             "，只用關鍵字搜尋" if args.bm25_only else "")
    application.run_polling(allowed_updates=bot.ALLOWED_UPDATES)
    log.info("bot 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
