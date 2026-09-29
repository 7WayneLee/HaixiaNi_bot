# Gemini（Vertex AI）問答

預設仍用 Claude Opus 5.5。Gemini 走 GCP Vertex AI 的計費端點，不使用 AI Studio 免費額度。`google-genai==2.25.0` 使用 `vertexai=True`、專案 `vmdemo1-507014`、`location=global` 和 API `v1`；這兩個模型在 `us-central1` 會回 404。

## 認證與切換

movie-nas 使用 Compute Engine 預設服務帳號提供的 Application Default Credentials（ADC），不需 Gemini API 金鑰；服務帳號須有呼叫 Vertex AI 模型的權限。查詢向量也沿用現有的 Vertex 認證。Claude 模型仍使用 `.env` 裡的 `ANTHROPIC_API_KEY`。

```sh
.venv-index/bin/python -m pip install -r requirements.txt
.venv-index/bin/python scripts/ask.py --model gemini-3.8-flash "少陽病的提綱？"
.venv-index/bin/python scripts/ask.py --model gemini-3.1-pro-preview "少陽病的提綱？"
```

Telegram 輸入 `/model` 會開啟 Claude／Gemini 二級按鈕選單；用 `/model opus`、`/model sonnet`、`/model gemini-flash`、`/model gemini-pro` 也能切換。換模型會開新對話。搜尋器仍由同一個 worker 執行緒共用；Gemini provider 只在第一次切到 Gemini 時載入。`/cost` 與每日預算使用同一份 JSONL 紀錄。

## 思考、工具與差異

預設 `--gemini-thinking default` **不送** `thinking_config`，由模型決定思考程度：3.8 Flash 預設 `MEDIUM`，3.1 Pro 預設 `HIGH`。可選 `low`、`medium`、`high`，CLI 用 `--gemini-thinking medium`，Telegram 服務啟動參數相同；程式用 `ThinkingConfig(thinking_level="MEDIUM")`。這兩個模型都不支援 `MINIMAL`。兩個模型均保留思考，思考 token 算輸出費用，也佔 `max_output_tokens=8192` 的上限。Claude 的 `--effort`、`--fallback`、`block_binding` 設定不會送給 Gemini。

Gemini 與 Claude 使用同一份 `data/system_prompt.md`，也共用 `search`（含經典原文）、`read_context` 和 `classic_commentary` 的驗證與執行。Gemini 關閉 SDK 自動 function calling，自行處理最多 8 輪；同回合多個工具結果一起回送。模型回傳的完整 `Content` 原樣追加到對話歷史，保留 Gemini 3 function call 的 thought signature。工具結果後若得到非安全擋下的空白答案，會在同一段只附加的歷史中要求 Gemini 依規定格式補答一次，這次不允許再呼叫工具；補答的用量照常計費並記在 notes。安全原因擋下時不執行工具，回覆友善說明。

## 價格與紀錄

以下是 2026-09-29 提供的 Vertex AI 價格，每百萬 token／美元；Flash 優惠到 2026-12-31，Pro 價格適用提示不超過 20 萬 token。價格變更時可用 CLI `--prices` 覆蓋。

| 模型 | 輸入 | 快取輸入 | 輸出（含思考） |
|---|---:|---:|---:|
| `gemini-3.8-flash` | $0.75 | $0.075 | $3.75 |
| `gemini-3.1-pro-preview` | $2.00 | $0.20 | $12.00 |

`usage_metadata.prompt_token_count` 包含快取輸入；計費時先扣除 `cached_content_token_count`，再將快取按快取價計。`candidates_token_count` 與 `thoughts_token_count` 都按輸出價計；`tool_use_prompt_token_count` 若有值，按輸入價另計。寫入 `~/haixia-bot-logs/answers.jsonl` 的欄位與 Claude 相同，`model` 是 Gemini 模型名，`usage` 額外列出候選、思考、工具提示 token。Gemini 目前未設定顯式快取，因此只有 API 回報快取 token 時才會使用快取價。Pro 提示超過 20 萬 token 時，這份價格表不適用，應先更新價格表。

Mac 離線量測：23,531 段索引、相同大小的模擬向量檔、兩個實際 SDK client、Telegram Application 與一次搜尋，最大 RSS 213.9 MiB。模擬向量內容全為零；這是本機記憶體量測，部署後仍應由管理者在 movie-nas 觀察實際常駐記憶體。

## 依據

- [Google Gen AI Python SDK：Vertex、手動 function calling 與關閉自動呼叫](https://googleapis.github.io/python-genai/)
- [Vertex AI：thought signature 必須隨完整模型回應送回](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures)
- [Google Gen AI SDK：思考與用量欄位](https://googleapis.github.io/python-genai/genai.html)
- [Vertex AI：Gemini 3.8 Flash 與 3.1 Pro 支援的思考程度與預設值](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-3-8-flash)
