# 第五步：Telegram bot

這是部署說明。2026-09-29 已部署到 movie-nas，以 systemd 服務 `haixia-bot` 執行。
bot 用 long polling，不需要網域或 HTTPS。輪詢接收一般訊息（`message`）與按鈕回呼（`callback_query`）。只有 `.env` 白名單中的 Telegram 使用者 ID 能得到回覆。

## 建立 bot 與設定

1. 在 Telegram 找 `@BotFather`，傳 `/newbot`，依提示設定名稱和以 `bot` 結尾的 username，保存它給的 token。可用 `/setcommands` 設定 `help`、`new`、`model`、`cost`、`source`。
2. 查自己的數字 Telegram 使用者 ID（例如用 Telegram 的 ID 查詢 bot）。要填的是**使用者 ID**，不是 username 或 chat ID。
3. 在 movie-nas 的 `~/HaixiaNi_bot/.env` 設定以下欄位，並執行 `chmod 600 ~/HaixiaNi_bot/.env`。檔案不進 repo；環境變數有值時優先於 `.env`。

   ```dotenv
   TELEGRAM_BOT_TOKEN=由_BotFather_取得的_token
   TELEGRAM_ALLOWED_USER_IDS=123456789
   ANTHROPIC_API_KEY=你的_Anthropic_金鑰
   DAILY_BUDGET_USD=3
   ```

   多個允許的 ID 用逗號分隔。組織層級 Anthropic 金鑰如有需要，另設 `ANTHROPIC_WORKSPACE_ID`；綁定 workspace 的金鑰不用。token 與金鑰不要貼到聊天、文件或 log。未列入白名單者不會收到訊息，也不會觸發 API 呼叫。

## 在 movie-nas 安裝服務

以下命令供審查後由指揮在 movie-nas 執行。本次不連線到 movie-nas。

```sh
cd ~/HaixiaNi_bot
git pull
.venv-index/bin/python -m pip install -r requirements.txt
.venv-index/bin/python scripts/telegram_bot.py --check
sudo cp deploy/haixia-bot.service /etc/systemd/system/haixia-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now haixia-bot.service
sudo systemctl status haixia-bot.service
```

索引預設在 `~/haixia-index-build`。`--check` 只檢查設定與索引，不連 Telegram 或 Claude。要只用關鍵字搜尋，可在前景執行時加 `--bm25-only`；systemd 範本使用預設的 Vertex 加 BM25。

查看、停止與更新：

```sh
journalctl -u haixia-bot.service -f
sudo systemctl stop haixia-bot.service
cd ~/HaixiaNi_bot && git pull
.venv-index/bin/python -m pip install -r requirements.txt
sudo systemctl restart haixia-bot.service
```

若 unit 檔有改動，更新時再複製一次並執行 `sudo systemctl daemon-reload`。修改 `.env` 後也要重啟服務。service 設定失敗時會在 journal 顯示設定名稱，但不顯示 token 或金鑰。重啟會清空記憶體裡的對話。

## 使用方式與費用

直接傳正體中文問題；連續問題會排隊。`/help` 或 `/start` 看用法，`/new` 開新對話。`/model` 會送出 inline 二級選單：第一層顯示目前模型，按鈕是「Claude」「Gemini」；第二層顯示該服務的兩個模型，目前模型標「（目前）」，另有「‹ 返回」。換層會編輯同一則訊息；選模型後改成確認文字並移除按鈕。只有白名單使用者能操作按鈕。`/model opus`、`/model sonnet`、`/model gemini-flash`、`/model gemini-pro` 文字捷徑仍可切換模型；切換只影響這個 chat 的下一題，並開新對話。`/cost` 看台灣時間今天與本月的題數和花費。Gemini 的 Vertex 設定、價格與思考參數見 [08-gemini.md](08-gemini.md)。醫案引用會隱藏姓名並附編號；只有白名單使用者能用 `/source 編號` 或「原文 編號」看原始標題、路徑、該段與前後段。對話閒置超過 6 小時或最後一個請求的輸入約超過 15 萬 token，下一題前會自動開新對話。

每題估計費用會寫到 `~/haixia-bot-logs/answers.jsonl`。常見題目約 Opus 5.5 US$0.20–0.26（約 NT$6–8）、Sonnet 5.5 US$0.10–0.14（約 NT$3–4）；Gemini 費用依實際用量計算，價格見 [08-gemini.md](08-gemini.md)。台幣以 1 美元＝32 元估算。`DAILY_BUDGET_USD` 預設為 3，按台灣時間午夜重新計算；當天已記錄花費達上限時，不再發出新的回答模型 API 請求。`/cost` 會顯示餘額。要調整上限，修改 `.env` 的 `DAILY_BUDGET_USD` 後重啟服務。
