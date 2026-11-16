# Gemini（Vertex AI）問答

預設仍用 Claude Opus 5.5。Gemini 走 GCP Vertex AI 的計費端點，不使用 AI Studio 免費額度。`google-genai==2.25.0` 使用 `vertexai=True`、專案 `vmdemo1-507014`、`location=global` 和 API `v1`。

## 模型陣容（2026-11-16 確認）

陣容統一由 `haixia/answer.py` 的 `GEMINI_LINEUP` 管理（目前 3 個模型都已在 `location=global` 實測）：

- **`gemini-4-hydrogen`（Gemini 4 Hydrogen）**：官方指定的 3.8 Flash 後繼（GA），對應 `/model gemini-flash`。
- **`gemini-4-argon`（Gemini 4 Argon）**：官方指定的 3.1 Pro 後繼（GA），對應 `/model gemini-pro`。不採用同價的 `gemini-4-carbon`，因為 Carbon 屬於 Google Frontier Model，依官方濫用監控規定會記錄提示與回應最多 30 天並可能人工審查，而本 bot 會送出含病人資料的醫案。
- **`gemini-3.8-flash`（Gemini 3.8 Flash）**：暫時保留，對應 `/model gemini-3.8-flash`。2026-12-31 前優惠價為 Hydrogen 的一半；2027-01-01 起與 Hydrogen 同價，屆時可移除。

已停用 `gemini-3.1-pro-preview`（預覽版，由 4 Argon 取代）；舊對話或 `/model` 指到已停用模型時，bot 會自動切回預設模型 Opus 5.5 並開新對話。

## 認證與切換

movie-nas 使用 Compute Engine 預設服務帳號提供的 Application Default Credentials（ADC），不需 Gemini API 金鑰；服務帳號須有呼叫 Vertex AI 模型的權限。查詢向量也沿用現有的 Vertex 認證。Claude 模型仍使用 `.env` 裡的 `ANTHROPIC_API_KEY`。

```sh
.venv-index/bin/python -m pip install -r requirements.txt
.venv-index/bin/python scripts/ask.py --model gemini-4-hydrogen "少陽病的提綱？"
.venv-index/bin/python scripts/ask.py --model gemini-4-argon "少陽病的提綱？"
.venv-index/bin/python scripts/ask.py --model gemini-3.8-flash "少陽病的提綱？"
```

Telegram 輸入 `/model` 會開啟 Claude／Gemini 二級按鈕選單（Gemini 子選單依序顯示 Gemini 4 Hydrogen、Gemini 4 Argon、Gemini 3.8 Flash）；也可用文字捷徑切換：`/model gemini-flash`（4 Hydrogen）、`/model gemini-pro`（4 Argon）、`/model gemini-3.8-flash` 或完整模型 id（Claude 為 `/model opus`、`/model sonnet`、`/model fable`）。換模型會開新對話。搜尋器仍由同一個 worker 執行緒共用；Gemini provider 只在第一次切到 Gemini 時載入。`/cost` 與每日預算使用同一份 JSONL 紀錄。

## 思考、工具與差異

預設 `--gemini-thinking default` 只送 `ThinkingConfig(include_thoughts=True)`，由各模型決定思考程度：`gemini-4-hydrogen` 預設 `HIGH`、`gemini-4-argon` 預設 `MEDIUM`、`gemini-3.8-flash` 用模型預設。可選 `low`、`medium`、`high`，CLI 用 `--gemini-thinking medium`，Telegram 服務啟動參數相同；程式會同時送 `include_thoughts=True` 與 `thinking_level="MEDIUM"`。這三個模型都不支援 `MINIMAL`（Hydrogen 另有 `XHIGH`，但目前 SDK `2.25.0` 尚無對應列舉值，先不開放）。只收集官方回傳的 thought part 作為摘要（實測 Argon 在 `MEDIUM` 不會回傳思考摘要）；完整 `Content` 仍原樣放回歷史，答案文字不含 thought part。思考 token 算輸出費用，也佔輸出上限：`gemini-4-hydrogen` 與 `gemini-4-argon` 的 `max_output_tokens` 為 `16384`，`gemini-3.8-flash` 為 `8192`。Claude 的 `--effort`、`--fallback`、`block_binding` 設定不會送給 Gemini。

Gemini 與 Claude 使用同一份 `data/system_prompt.md`，也共用 `search`（含經典原文）、`read_context` 和 `classic_commentary` 的驗證與執行。Gemini 關閉 SDK 自動 function calling，自行處理最多 8 輪；同回合多個工具結果一起回送。模型回傳的完整 `Content` 原樣追加到對話歷史，保留 function call 的 thought signature。工具結果後若得到非安全擋下的空白答案，會在同一段只附加的歷史中要求 Gemini 依規定格式補答一次，這次不允許再呼叫工具；補答的用量照常計費並記在 notes。安全原因擋下時不執行工具，回覆友善說明。

## 價格與紀錄

以下是 **2026-11-16** 依 Vertex AI 官方價格頁確認的 `global` 端點價格，每百萬 token／美元（目前陣容均無 20 萬 token 以上的長脈絡加價）。使用預設價格表時，`PRICE_CHANGES` 會依 UTC 日期自動切換到期後的新價格；用 CLI `--prices` 自訂價格表時則依自訂表計價：

| 模型 | 輸入 | 快取輸入 | 輸出（含思考） | 調價說明 |
|---|---:|---:|---:|---|
| `gemini-4-hydrogen` | $1.50 | $0.075 | $7.50 | 無 |
| `gemini-4-argon` | $2.00 | $0.10 | $10.00 | 促銷價（5 成 credits 回饋）到 2026-11-30；2026-12-01 起 $4.00 / $0.20 / $20.00 |
| `gemini-3.8-flash` | $0.75 | $0.075 | $3.75 | 優惠價到 2026-12-31；2027-01-01 起 $1.50 / $0.15 / $7.50 |

`usage_metadata.prompt_token_count` 包含快取輸入；計費時先扣除 `cached_content_token_count`，再將快取按快取價計。`candidates_token_count` 與 `thoughts_token_count` 都按輸出價計；`tool_use_prompt_token_count` 若有值，按輸入價另計。寫入 `~/haixia-bot-logs/answers.jsonl` 的欄位與 Claude 相同，`model` 是 Gemini 模型名，`usage` 額外列出候選、思考、工具提示 token。Gemini 目前未設定顯式快取，因此只有 API 回報快取 token 時才會使用快取價。

Mac 離線量測：23,531 段索引、相同大小的模擬向量檔、兩個實際 SDK client、Telegram Application 與一次搜尋，最大 RSS 213.9 MiB。模擬向量內容全為零；這是本機記憶體量測，部署後仍應由管理者在 movie-nas 觀察實際常駐記憶體。

## 依據

- [Google Gen AI Python SDK：Vertex、手動 function calling 與關閉自動呼叫](https://googleapis.github.io/python-genai/)
- [Vertex AI：thought signature 必須隨完整模型回應送回](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures)
- [Google Gen AI SDK：思考與用量欄位](https://googleapis.github.io/python-genai/genai.html)
- [Vertex AI（Gemini Enterprise Agent Platform）：模型總覽與 Gemini 4 指南](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models)
- [Vertex AI：濫用監控與 Frontier Model 資料記錄說明](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/abuse-monitoring)
- [Vertex AI：定價](https://cloud.google.com/vertex-ai/generative-ai/pricing)
