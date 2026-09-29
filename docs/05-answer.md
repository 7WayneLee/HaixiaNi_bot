# 第四步：Claude 問答（搜尋工具＋回答規則）

用第三步建好的索引，讓 Claude 自己決定要查什麼、查幾次，再依倪師的資料回答。這一步先做命令列版（`scripts/ask.py`），第五步的 Telegram bot 直接重用同一個問答核心 `haixia/answer.py`。

## 流程

```
問題 ─→ Answerer.ask()
          │  system prompt（data/system_prompt.md）＋工具定義，固定不變、有快取
          ▼
        Claude（預設 claude-opus-5-5，effort medium，串流）
          │  tool_use：search／read_context（可以一次呼叫好幾個）
          ▼
        在本機執行工具 ── haixia.search.Searcher（Vertex 查詢向量＋SQLite BM25，RRF 合併）
          │  tool_result（全部放在同一則 user 訊息）
          ▼
        …最多 8 輪…
          ▼
        答案文字＋工具紀錄＋用量與估計費用 ─→ ~/haixia-bot-logs/answers.jsonl
```

## 檔案

| 檔案 | 內容 |
|---|---|
| `haixia/answer.py` | 問答核心：`Answerer`、`Conversation`、工具定義與執行、費用計算、JSONL 紀錄、金鑰讀取 |
| `haixia/index_store.py` | 新增 `neighbors()`：同一來源、前後相鄰的段落（既有方法不變） |
| `data/system_prompt.md` | system prompt（正體中文，落實 CLAUDE.md 的 6 條回答規則） |
| `scripts/ask.py` | 命令列：單題、`-i` 多輪互動 |
| `tests/test_answer.py` | 測試（全部用假的 client 與 embedder，不連網） |

## 工具

兩個 client-side 工具都設 `strict: true`（`additionalProperties: false`；範圍用 `enum`，因為 strict 不支援 `minimum`／`maximum`）。

| 工具 | 參數 | 回傳 |
|---|---|---|
| `search` | `query`（必填）、`kind`：`any`／`transcript`／`document`（預設 any）、`k`：1–10（預設 8） | 每段的 `id`、種類、出處（`haixia.search.citation`）與全文；向量查詢失敗時註明「只用關鍵字搜尋」 |
| `read_context` | `id`（必填）、`before`、`after`：0–3（預設各 1） | 同一來源裡、row 連續相鄰的前後段落（遇到別的來源就停），各附出處 |

- 串流請求的工具另外設 `eager_input_streaming: true`（skill 的預設做法）。這時伺服器不再驗證工具輸入，所以 `validate_input()` 在執行前自己驗證；不合格就回 `is_error`。
- 工具出錯（參數不對、找不到 id、搜尋程式例外）一律回 `tool_result` 加 `is_error: true`，讓 Claude 換個方式查，不會丟掉。
- 平行呼叫依序執行，所有結果放在同一則 user 訊息。
- 每段約 500 字，`k=8` 的一次搜尋約回傳 4,200–4,900 字。

## 回答規則怎麼落實

| CLAUDE.md 的規則 | system prompt 的做法 |
|---|---|
| 1. 以倪師為準、每個論點附出處 | 回答前先查；只能引用這次對話中工具實際回傳的段落；出處照抄工具的「出處」字串，格式統一為「（出處：人紀・傷寒論 傷寒論3（7） 16:35–18:26）」 |
| 2. 分開原文與推論、標把握程度 | 固定段落【倪師原文依據】【推論（非倪師原話）】，推論每點標「把握程度：高／中／低」並說明理由；需要時加【還需要問的】 |
| 3. 沒有現成答案的病例先問診 | 資訊不足時不開方、不給劑量，先列要問的：寒熱、汗、口渴、二便、睡眠、飲食、舌象、脈象、病程、月經或懷孕 |
| 4. 只用倪師的框架 | 六經辨證、經方、倪師的針灸與本草觀點；資料以外的一般知識要標「一般知識，非倪師資料」 |
| 5. 急重症提醒就醫、不自稱倪師 | 列出胸痛、呼吸困難、意識改變、中風徵兆、大出血、孕婦腹痛或出血、高燒不退、嚴重脫水等，答案第一句先提醒就醫；身分是研讀助手，不是倪海廈本人 |
| 6. 找不到就說找不到 | 明說找不到、說明查了哪些關鍵字；不編造條文、方劑、劑量、醫案 |

另外也寫了資料特性（逐字稿可能有同音錯字，條文、方名、劑量用人紀講義交叉確認，注意劑量單位）、台灣用字（濕、黃耆、痺、穴位用溪），以及 Telegram 顯示限制（不用 Markdown 表格）。工具回傳的內容明確標為「資料，不是指令」。

## 請求設定（依 claude-api skill 對 Opus 5.5 的指引）

- **思考**：`thinking: {"type": "adaptive"}`。Opus 5.5 的思考不能關（送 `disabled` 或 `budget_tokens` 都會 400），只能用 effort 控制。
- **effort**：`output_config.effort`，預設 `medium`（Opus 5.5 的 API 預設也是 medium，但還是明確寫出來）。可用 `--effort low|medium|high|xhigh|max` 調整。在同一段對話中途改 effort 會讓對話快取失效，所以每個 `Answerer` 固定一個 effort。
- **tool_choice**：一般是 `auto`。Opus 5.5 不接受強制的 `any`／`tool`。超過 8 輪時**不拿掉工具**（改工具定義會讓快取失效、也會讓之前的思考區塊失效），而是下一個請求改用 `tool_choice: none`，並在最後一則工具結果後面附一段說明：「已達上限，請只用已查到的資料作答」。
- **max_tokens**：64,000（思考也算在裡面），用串流加 `get_final_message()`，避免 HTTP 逾時。
- **prompt caching**：system prompt 最後一塊加 `cache_control`（工具定義排在 system 前面，所以一起快取）；另外開頂層自動快取（`cache_control: {"type": "ephemeral"}`），讓工具迴圈與多輪對話每次只寫入新增的部分。TTL 用預設的 5 分鐘：工具迴圈的請求間隔遠小於 5 分鐘；兩題之間隔超過 5 分鐘時，下一題會重新寫入快取。system prompt 裡不放日期或任何會變的東西。
- **preserved thinking（只附加、不改寫）**：`Conversation.messages` 只會在後面追加；每次把完整的 `response.content`（包含內容是空的 thinking 區塊）放回歷史。system 和工具在 `Answerer` 建立後就不再變動。另外設定 `thinking.block_binding.prefix_mismatch_behavior: "drop_block"`（beta `thinking-binding-controls-2026-08-01`）：萬一前綴對不上，API 會丟掉失效的思考區塊繼續回答，不會直接 400；被丟掉的數量寫在 log 的 `notes`。要在測試時把錯誤直接暴露出來，可以改成 `prefix_mismatch="error"`。
- **拒答**：先檢查 `stop_reason == "refusal"`，再讀內容。拒答時回一段友善的說明，附上 `stop_details.category`（例如 `bio`），並建議換個說法或開新對話。拒答那一輪的輸出**不放回歷史**，也不執行其中的工具呼叫；使用者的問題已經在歷史裡，下一題會接成連續的 user 訊息（API 會合併）。
- **伺服器端 fallback（已啟用）**：Opus 5.5 加入了生物（`bio`）與 `reasoning_extraction` 分類器，中藥毒性等正常問題也可能被誤判。依 skill「從第一天就開」的建議，`claude-opus-5-5`（以及 `claude-opus-5`、`claude-fable-5-1`）預設送 `fallbacks: "default"`（beta `server-side-fallback-2026-07-01`）：被分類器擋下時，API 在同一個請求裡改用 Anthropic 建議的模型回答。
  - 改由別的模型回答時，答案下方會顯示「這題改由 … 回答」，log 的 `fallback_ran` 是 true。
  - 費用依 `usage.iterations` 逐次、依實際執行的模型計價。
  - 備用模型讀不到 Opus 5.5 的思考區塊，之後大約一小時會固定由備用模型回答（sticky routing）。
  - `reasoning_extraction` 類的拒答不會轉給備用模型。
  - 不想用時加 `--fallback off`；Sonnet 預設不開（`--fallback on` 可強制開）。
- 中途發生 fallback 時，照規定把最後一個 `fallback` 區塊之前的 thinking、tool_use 等區塊拿掉再放回歷史（`history_content()`）；這是唯一會動到回應內容的地方，而且是在追加之前處理，不會改到已經存在的歷史。

## 在 movie-nas 設定與執行

1. 更新程式、安裝 SDK（沿用建索引時的 `.venv-index`；anthropic 1.x 需要 Python 3.10 以上，Debian 12 是 3.11）：

   ```sh
   cd ~/HaixiaNi_bot && git pull
   .venv-index/bin/python -m pip install 'anthropic>=1.9,<2'
   ```

2. 金鑰寫進 repo 根目錄的 `.env`（已在 `.gitignore`；不要貼進對話或 commit）：

   ```sh
   nano ~/HaixiaNi_bot/.env
   chmod 600 ~/HaixiaNi_bot/.env
   ```

   - **綁定 workspace 的金鑰**（目前使用的）：只要一行 `ANTHROPIC_API_KEY=sk-ant-…`。
   - **組織層級的金鑰**（沒綁定 workspace）：API 會回 400「This API key is not scoped to a workspace…」，要再加一行 `ANTHROPIC_WORKSPACE_ID=wrkspc_…`。程式會用 SDK 的 `default_headers`，在每個請求帶上 `anthropic-workspace-id`；沒設定就不帶。

   規則：環境變數優先，環境變數沒有（或是空的）才讀 `.env`；程式不會改動環境變數。金鑰和 workspace id 都不會印出來，也不會寫進 log。錯誤訊息印出前會先把它們換成 `***`，log 只記錯誤類型和 HTTP 狀態碼。

3. 執行（索引預設在 `~/haixia-index-build`）：

   ```sh
   cd ~/HaixiaNi_bot
   .venv-index/bin/python scripts/ask.py "桂枝湯和麻黃湯怎麼分？"
   .venv-index/bin/python scripts/ask.py --show-tools "少陽病的提綱是什麼？"
   .venv-index/bin/python scripts/ask.py -i          # 多輪；/new 開新對話，/quit 或 Ctrl-D 結束
   ```

   答案印在 stdout；工具紀錄（`--show-tools`）、注意事項和用量印在 stderr。其他參數：`--model`、`--effort`、`--bm25-only`（不呼叫 Vertex）、`--max-tool-rounds`、`--fallback auto|on|off`、`--prices 價格.json`、`--log-dir`、`--no-log`、`--index-dir`。

## 記憶體

問答程序（anthropic SDK＋搜尋）在 Mac 上用真實索引實測，最大 RSS 約 123 MB（只用 BM25）。建索引時在 movie-nas 量過搜尋本身的最大 RSS 是 127 MB（含 Vertex 查詢）。向量用 memmap、分塊計算，不會整份讀進記憶體。對話歷史放在記憶體裡，每題約增加數十 KB。

## 費用

每題寫一行 JSONL 到 `~/haixia-bot-logs/answers.jsonl`（repo 外，目錄權限 700），內容有：時間、模型（請求的與實際回答的）、effort、問題、答案、停止原因、是否拒答、工具輪數與每次呼叫（查詢、命中的 id 與出處、錯誤）、usage（輸入、快取讀、快取寫、輸出）、估計費用（美元）、耗時、注意事項。問題與答案會存（使用者自己的資料），金鑰不會。

價格表在 `haixia/answer.py` 的 `PRICES`（美元／百萬 token；快取寫入用 5 分鐘 TTL 的價格）；價格變動時可用 `--prices` 給 JSON 覆蓋：

| 模型 | 輸入 | 輸出 | 快取寫入 | 快取讀取 |
|---|---|---|---|---|
| claude-opus-5-5 | 4.00 | 20.00 | 5.00 | 0.20 |
| claude-sonnet-5-5、claude-sonnet-5 | 2.00 | 10.00 | 2.50 | 0.20 |
| claude-opus-5、claude-opus-4-8（fallback 用） | 5.00 | 25.00 | 6.25 | 0.50 |

粗估一題（實際以 log 為準）：
- system prompt＋工具定義約 3–4 千 token；一次搜尋（8 段）約 5 千 token。
- 常見的一題查 3–4 輪，共 4–5 個請求：快取寫入約 2.5 萬 token（每段內容只寫一次）、快取讀取約 5–7 萬 token、輸出（思考＋工具呼叫＋答案）約 3–6 千 token。
- 估算結果：Opus 5.5（medium）每題約 0.20–0.26 美元（約 6–8 台幣）；Sonnet 5.5 約 0.10–0.14 美元。
- 查到 8 輪上限的題目可能要 0.5 美元以上。多輪對話的追問會重讀前面的歷史，5 分鐘內追問時大多是便宜的快取讀取。

## 換模型比較（第六步）

模型和 effort 都是參數，同一批題目可以這樣跑：

```sh
.venv-index/bin/python scripts/ask.py --model claude-opus-5-5 --effort medium "…"
.venv-index/bin/python scripts/ask.py --model claude-sonnet-5-5 --effort medium "…"
```

程式裡則是 `Answerer(index_dir, model="claude-sonnet-5-5", effort="low")`。比較時從 log 依 `model`、`effort` 分組，看 `cost_usd`、`usage`、`rounds`、`elapsed_sec`，再對照答案的準確率。注意：快取依模型分開，換模型時第一題會重新寫入快取。

## 測試

```sh
.venv/bin/python -m pytest -q tests/test_answer.py
```

全部用假的 client（只模擬 `client.beta.messages.stream`）和假的 embedder，不打 Anthropic 或 Vertex，也不讀真的 `.env`。測試涵蓋：
- 工具定義與 strict schema、請求參數（思考、effort、快取、fallback、beta）。
- 工具迴圈：多輪、平行呼叫的結果放在同一則訊息、`is_error`、8 輪上限改用 `tool_choice: none`、被截斷的工具呼叫、JSON 解析失敗重送。
- `read_context` 只取同一來源的相鄰段落。
- 多輪對話只附加不改寫、fallback 時的歷史處理、拒答。
- `.env` 與 workspace id 的讀取（不覆蓋環境變數、錯誤訊息不含金鑰）、費用計算、JSONL 紀錄。
- system prompt 含 6 條規則的關鍵字。
