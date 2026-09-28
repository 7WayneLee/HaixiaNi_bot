# 第三步：校正逐字稿

這一步在使用者的 Mac 上執行。輸入是 `haixia.transcript/1` 的 Whisper ASR JSON；輸出是 `haixia.corrected/1`，保留每段原文 `text_asr`、校正標記與搜尋／耗時紀錄。原始 ASR 不會被覆寫。

## 前置條件

- 安裝並登入 Antigravity CLI；`agy models` 應列出 `gemini-3.8-flash-high`。
- 若使用 Codex，安裝並登入 Codex CLI，並確認 `orca account list --json` 能讀取 Codex 的 session 與 weekly 額度。
- 若使用 Claude，Claude Code CLI 要用訂閱帳號登入（`claude auth`），並確認 `orca account list --json` 能讀取 `claude` 的 session 與 weekly 額度。
- 使用本專案的 `.venv`，並確認磁碟有空間存 ASR、校正版和工作快取。
- 在 repo 外指定 `--work-dir`，例如 `/tmp/haixia-correct-work`。程式會建立 `ws/.agents/` 的 hook 設定；`ws/` 不放逐字稿或其他檔。

`agy -p` 在非互動模式遇到需人工核准的工具時，可能直接沒有輸出。工作目錄 hook 只准每次對話最多 5 次 `search_web`，並拒絕開網頁、執行指令和讀寫檔案。模型會看到拒絕理由並繼續校正。程式啟動前會以假工具呼叫檢查 hook。

## 下載、試跑、全量與上傳

```bash
scripts/sync_transcripts.sh pull "$HOME/haixia-asr"

# 先檢查提示詞，不耗用 Antigravity 額度。
.venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --limit-hours 10 --dry-run

# 先跑約 10 小時音訊。MacBook 要保持開蓋；闔蓋仍會睡眠。
caffeinate -i .venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --limit-hours 10 --jobs 4

# 全量可一次指定多個課程；已完成的輸出會跳過。
caffeinate -i .venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --jobs 4 \
  --include '影片/02 針灸/' --include '影片/05 傷寒論/'

# 也可完全不加 --include，處理 --in-dir 目前的全部檔案。
caffeinate -i .venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --jobs 4

# 檢查後只重跑 failed／partial 段，其餘段沿用快取或既有校正版。
caffeinate -i .venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --jobs 4 --retry-failed \
  --include '影片/05 傷寒論/'

scripts/sync_transcripts.sh push "$HOME/haixia-corrected"
```

`pull` 可以加 `--include '影片/02 針灸/'`，只傳回指定來源前綴；校正 CLI 也可重複加 `--include`，或用 `--files` 指定相對 JSON 路徑清單（每行一個）。不加 `--include` 就會處理 `--in-dir` 的全部 JSON。轉錄完成並拉回新檔後，按 Ctrl-C 停止校正，再用同一組目錄重跑；已完成的段會沿用，不必從頭開始。檔案很多時可依課程分批執行，降低 Mac 記憶體用量，也方便逐批驗收。`--limit-hours` 依檔名排序累加整檔音訊時數，到達門檻即停。`--retry-failed` 只重跑既有校正版中 `failed` 或 `partial` 的段；`--force` 會重做所有已輸出段。規則、詞表或提示詞改變時，尚未輸出的檔會自動捨棄舊段落快取，已輸出的檔需加 `--force` 重做。

## Antigravity 與 Codex 接力或並行

預設 `--engines agy`，維持原本只用 Antigravity 的行為。兩個引擎共用依檔案順序排列的段落佇列：

```bash
caffeinate -i .venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --engines agy,codex \
  --agy-jobs 4 --codex-jobs 4 --codex-model gpt-6-sol --codex-effort medium
```

`--codex-mode relay` 是預設：Antigravity 正常工作時，Codex 不取新段；Antigravity 因額度或斷路器暫停時，Codex 接手。Antigravity 恢復後，Codex 完成手上的段便等待下一次暫停。只選 `--engines codex` 時 Codex 直接工作。要讓兩邊同時取段，改用 `--codex-mode parallel`。`--jobs` 仍是 `--agy-jobs` 的別名。

每段只由一個引擎持有並完成重試。額度暫停是例外：尚未成功的段會放回佇列前端，供另一個引擎從頭處理。兩個引擎使用完全相同的校正提示詞。每段中繼資料記錄 `engine`、`model`、`effort`；一份檔案若由兩個引擎完成，`correction.tool` 為 `mixed`。

Codex 在 repo 外的 `工作目錄/codex-ws/` 空資料夾執行，stdin 關閉，網路搜尋設為 `cached`；這台機器的 `live` 搜尋會遇到授權錯誤。Codex 每次呼叫前檢查 Orca 額度，最多每 60 秒查一次。週額度達 `--codex-weekly-max`（預設 80%）時，本次執行停用 Codex，Antigravity 繼續；session 達 `--codex-session-max`（預設 85%）時，Codex 暫停到 `resetsAt` 後 2 分鐘。Codex 自己回報 usage limit、HTTP 429，或「Your workspace is out of credits. Add credits to continue.」（2026-09-26 實測是 5 小時額度用完，重設後就恢復），都算額度錯誤：按 session 重設時間加 2 分鐘暫停，重設時間不明時用指數退避；不算段落失敗，也不觸發斷路器。資料讀不到時保守暫停 15 分鐘。websocket 斷線後接 401 的錯誤視為暫時性連線錯誤重試，並非額度用完。錯誤訊息內的 `sk-` 金鑰字串會在 log、狀態與快取中遮蔽。

## Claude Code 接手（Claude Opus 5.5）

Antigravity 沒額度的空檔，也可以讓 Claude Code CLI 接手。`--engines` 可以是 `agy`、`codex`、`claude` 以逗號分隔的任意組合，例如：

```bash
caffeinate -i .venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --engines agy,codex,claude --codex-mode relay \
  --agy-jobs 4 --codex-jobs 4 --claude-jobs 4 --quota-poll-min 10
```

參數：`--claude-jobs`（預設 4）、`--claude-model`（預設 `claude-opus-5-5`）、`--claude-effort`（預設 `medium`）、`--claude-session-max`（預設 80）、`--claude-weekly-max`（預設 100，也就是不設限）、`--claude-timeout`（預設 300 秒）、`--claude-max-budget-usd`（預設 1.0）。提示詞和 Antigravity、Codex 完全相同，`prompt_sha256` 不變。

### 2026-09-27 實測（針灸5（2），8 段，`--claude-jobs 2`）

- 8 段全部 ok。正常的段每段 8–70 秒（多半 30–45 秒），只有 1 段搜尋 1 次；1 段第一次少一行，重跑後合格。
- 第 1 段的第一次呼叫**卡了 15 分鐘沒有結果**（當時的逾時是 900 秒），重跑 34 秒就完成。卡住的這段時間 5 小時額度從 38% 漲到 61%，同時間只有它在跑批次（指揮和 worker 也在用同一個額度，分不開），所以失控的呼叫可能很耗額度。因此 Claude 另設 `--claude-timeout`（預設 300 秒）和 `--claude-max-budget-usd`（預設 1 美元）。`--max-budget-usd` 在訂閱登入下也有效，但每輪回應結束才檢查，擋不住單一回應一直不結束，那種情況靠逾時。
- 評分（原檔 10:00–14:00，字錯率／中醫詞召回）：原始 ASR 27.5%／15%，Antigravity 18.7%／100%，Claude 19.0%／92%（漏 1 個「壓痛點」）。
- 額度：正常段依 API 牌價估算每段約 0.08–0.23 美元（平均約 0.15）。串流回報的 5 小時用量在前 8 次呼叫完成期間從 26% 漲到 38%，含卡住的呼叫與其他 session，所以每段最多約 1.5%；週額度同一期間從 72% 到 73%（只有整數精度），每段約 0.25% 以下。

### 接力

`--codex-mode` 同時管 Codex 和 Claude。relay（預設）時，Codex 和 Claude 只在 Antigravity 暫停（額度、斷路器、帳號不符）或沒啟用時取新段；Antigravity 恢復後，它們做完手上的段就停止取新段。Codex 和 Claude 都能用時，兩者都會取段。三個引擎的暫停互相獨立：某個引擎因額度暫停時，手上還沒成功的段放回佇列前端，由其他引擎接手。暫停時間到了，該引擎會先確認額度再恢復取段。

### 呼叫方式與隔離

每段呼叫一次：

```
claude -p --model claude-opus-5-5 --effort medium --no-session-persistence \
  --output-format stream-json --verbose --max-budget-usd 1 \
  --setting-sources "" --settings '{"autoMemoryEnabled": false, "hooks": {"PreToolUse": …gate.py…}}' \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --tools WebSearch --allowedTools WebSearch --permission-mode dontAsk \
  --disable-slash-commands --no-chrome -- <提示詞>
```

- 工作目錄是 repo 外的空資料夾 `工作目錄/claude-ws/`，stdin 接 `/dev/null`，不保存 session。
- `--setting-sources ""`：不載入使用者、專案的設定檔，所以使用者的 hooks（例如 Orca 的狀態回報）和外掛都不會載入；`--strict-mcp-config` 加空的 MCP 設定，不載入任何 MCP；`--disable-slash-commands` 關掉技能；`autoMemoryEnabled: false` 關掉自動記憶。2026-09-27 實測（Claude Code 2.1.283）：這個組合在訂閱登入（OAuth）下可以正常呼叫，init 事件裡的工具只有 `WebSearch`，沒有使用者的外掛、MCP、技能。
- **不用 `--bare`**：它只認 `ANTHROPIC_API_KEY`，不讀訂閱登入，實測會回「Not logged in · Please run /login」。也絕對不用 `--dangerously-skip-permissions`。
- 程式會拿掉呼叫端的 `CLAUDECODE`、`CLAUDE_CODE_SESSION_*` 等 session 變數和 `ORCA_AGENT_*`，避免批次的 claude 被當成外層 session 的一部分。
- 逾時（`--claude-timeout` 加寬限）與 Ctrl-C 的處理和 Antigravity、Codex 一樣，殺整個程序群組。逾時的嘗試會在快取記下收到幾個事件、最後幾個事件的種類和已輸出的字數。
- `--max-budget-usd`（`--claude-max-budget-usd`）超過時 result 是 `error_max_budget_usd`，算一般錯誤、照重試次數重跑。

### 工具與搜尋上限

- `--tools WebSearch` 讓模型只看得到網路搜尋；`--permission-mode dontAsk` 加 `--allowedTools WebSearch`，其他需要核准的動作一律自動拒絕。
- `--settings` 帶 PreToolUse hook（`tools/claude_hook/gate.py`，啟動時複製到 `工作目錄/claude-hook/`）：每個 session 最多放行 `--max-search` 次（預設 5）WebSearch，第 6 次起拒絕並告訴模型直接依聲音與上下文判斷；WebFetch、Bash 等其他工具一律拒絕。Claude 可能在同一則訊息同時搜尋好幾次，所以計數檔有上鎖。每次工具呼叫都記到 `claude-hook/audit.jsonl`。程式啟動前會以假工具呼叫檢查 hook。
- 搜尋次數從 stream-json 算：`WebSearch` 的 `tool_use` 中沒被拒絕的次數記為 `searches`，被 hook 拒絕的記為 `searches_denied`。每次嘗試的快取（`chunks/…/NNNNN.attemptK.json`）另記 `hook_searches`（稽核紀錄的放行次數）、`queries`（搜尋字串）、`usage`（token 數、`web_search_requests`、`total_cost_usd` 等）和 `rate_limit`（最後一個 rate_limit_event）。出現 WebSearch 以外的工具呼叫時 log 會警告。

### 額度管控

- 每次取段前（最多每 60 秒一次）讀 `orca account list --json` 的 `result.rateLimits.claude.session` 和 `weekly`（`usedPercent`、`resetsAt`）。每次呼叫的 stream-json 也有 `rate_limit_event`（`five_hour`、`seven_day` 的 `utilization`），比 Orca 即時，程式一併採用。同一個額度週期內用量只增不減，所以兩個來源取重設時間較晚的週期，同一週期取較大值。
- session ≥ `--claude-session-max`（預設 80%）：Claude 暫停到 session 重設時間加 2 分鐘；重設時間已過但資料還沒更新時，一分鐘後再讀。
- `--claude-weekly-max` 預設 100，表示不設限；設成小於 100 時，週額度達上限就在本次執行停用 Claude。
- Claude 回報 usage limit、rate limit（例如「You've hit your limit」、`rate_limit_event` 的 `status` 為 `rejected`、HTTP 429）算額度錯誤，不算段落失敗、不觸發斷路器：
  - 被拒的是 6 小時內會重設的額度（5 小時額度）：暫停到它的重設時間加 2 分鐘；
  - 被拒的是週額度：從 5 分鐘起指數退避、最長每 60 分鐘重試（使用者可能用重設券，不等到重設時間）；
  - 沒有重設時間：依 Orca 的 session 重設時間加 2 分鐘暫停，也不知道時就指數退避。
- 讀不到額度資料時保守暫停 15 分鐘。
- 注意：已經在跑的呼叫不會被中斷，所以 session 用量可能超過上限最多「同時呼叫數 × 每段用量」。指揮和寫程式的 worker 也用同一個 Claude 額度，需要時可降低 `--claude-jobs` 或 `--claude-session-max`。

### 紀錄

每段中繼資料記 `engine`=`claude-cli`、`model`、`effort`；`correction.tool` 在整份都由 Claude 完成時是 `claude-cli`，混用時是 `mixed`。`status.json` 的 `engines.claude` 有狀態、暫停時間、原因、完成段數、平均耗時，以及最後讀到的 `session_used_percent`、`weekly_used_percent`、`session_resets_at`、`weekly_resets_at`。舊格式（沒有 `engine` 等欄位）的校正版仍然有效。

## 重做某個引擎的段（`--redo-engine`）

全量跑完後，如果某個引擎做的段品質較差（例如 Claude 幾乎不上網查證），可以只把那些段交給別的引擎重做，其他段一律不動：

```bash
# 先看要重做多少：只替目標段寫提示詞，印出「重做：N 檔、M 段、H 音訊小時」，不呼叫任何引擎。
.venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --engines agy --redo-engine claude-cli --dry-run

# 試跑 20 段，確認沒問題再拿掉 --redo-limit 跑全部。
.venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --engines agy --agy-jobs 4 --quota-poll-min 10 \
  --redo-engine claude-cli --redo-limit 20
```

- **選檔**：只看 `--out-dir` 已有、能通過驗證、而且至少有一段 `engine` 是指定引擎的校正版（舊格式沒有 `engine` 的段算 `antigravity-cli`）。沒有校正版的檔不會新做；`--include`、`--files` 照常可以縮小範圍。
- **安全檢查**：用 ASR 原檔重新切段，段數、每段起訖、每個 segment 的 ASR 文字與起訖，以及 `prompt_sha256`、`max_search` 都要跟既有校正版一致。任何一項不符就記一行錯誤、整檔略過，檔案不動。
- **只重做目標段**：其他段直接沿用既有校正版的文字與中繼資料，不呼叫引擎。目標段照一般流程交給 `--engines` 重做（快取、重試、驗收、額度暫停、帳號檢查、`resume-now`、Ctrl-C 都照舊）。快取裡指定引擎的結果不算數；快取裡 `partial`、`failed` 的結果會重跑。
- **取代規則**：重做結果是 `ok` 才取代，該段中繼資料多一個 `replaced`，記下被取代那一版的 `engine`、`model`、`effort`、`searches`、`elapsed_sec`；`partial`、`failed` 或發生例外時保留原版（文字與中繼資料原封不動），log 記一行「保留原本 … 的版本」。其他段原有的 `replaced` 會保留。
- **寫檔**：一個檔的目標段全部有結果（取代或保留）才重建整份寫回原位；`correction.tool` 依各段引擎重算，`created_at` 更新，`model` 沿用既有校正版。被中斷而沒做完的檔不寫，下次再跑仍會被選到。
- **進度**：`status.json` 只算目標段（段數、音訊小時、檔數），`watch_correction.py` 不用改；結束摘要多一行「重做：取代 X 段、保留原版 Y 段、未完成 Z 段」。有段保留原版、有檔因安全檢查略過，或沒做完時，結束碼是 1。
- **限制**：`--engines` 不能包含要重做的那個引擎；不能跟 `--force`、`--retry-failed`、`--limit-hours` 一起用（限制數量用 `--redo-limit`）。

## 暫停、續跑與監看

每段、每次嘗試會存到 `工作目錄/chunks/`，完成檔則寫到 `--out-dir`。按 Ctrl-C 後，程式立即停止派新段，向進行中的 CLI 程序群組送出 SIGTERM；5 秒後仍未結束就送 SIGKILL。被中止的段不寫入快取，用同一組目錄重跑即可續跑。退出碼 0 表示無 failed 段，1 表示有 failed 段沿用 ASR 或段落未完成，2 表示手動中止。

Antigravity 的額度每 5 小時重設一次，所有模型共用。額度錯誤若提供 `Resets in` 倒數，Antigravity worker 會暫停至預計重設時間再加 2 分鐘（加了 `--quota-poll-min` 時最多只停那麼久，見下方「換 Gemini 帳號接力」）；多個 worker 回報不同時間時取最晚的。倒數缺失或不合理時，才從 5 分鐘起指數退避，最多 60 分鐘。額度暫停不扣段落重試次數。網路錯誤、空輸出或其他 CLI 錯誤若連續三次出現，也會啟動該引擎的斷路暫停；暫停中其他 worker 回報的錯誤不會提高退避等級。成功呼叫會清除連續錯誤計數。`工作目錄/logs/correct.log` 有每段結果、仍有 failed 段的檔名和每五分鐘進度；`工作目錄/status.json` 有 `running`、`paused`、`finished`、`aborted` 狀態、PID、啟動時間、最後完成段落時間、已知額度重設時間、下次嘗試時間、計數與最後錯誤。

雙引擎模式下，額度暫停、斷路器和連線錯誤各自計算，不會暫停另一個引擎。`status.json` 的 `engines` 區塊記錄各引擎狀態、暫停時間、原因、完成段數與平均耗時；Codex 另記最近的 session／weekly 使用百分比。`codex_mode` 和 `active_engines` 顯示接力／並行模式及目前正在呼叫的引擎。監視程式對非額度原因停止的引擎發警報；停止原因是額度錯誤（含 `RESOURCE_EXHAUSTED`、`429`、`quota` 或「額度」）時只記 INFO，Codex 達週上限也只記 INFO。Codex、Claude 因額度暫停時，每次暫停在 `logs/watch.log` 記一行 INFO（「claude 引擎因額度暫停」），不發通知；斷路器暫停照舊由 log 的「連續 N 次呼叫失敗」發警報。校正程式中止或結束時，狀態檔會把各引擎標成 stopped 並沿用最後一次的錯誤，所以整體狀態是 `aborted` 或 `finished` 時不檢查引擎停止，只送結束通知。

建議在**另一個 Orca 終端機分頁**啟動不用 AI 的監視程式，檢查 PID、進度、log 與磁碟。它只讀校正資料，僅寫自己的 `logs/watch.log`；異常才發 ALERT。可選擇將警報及每日摘要送到 Orca Run；先把 `RUN_ID` 設成目標 Run ID：

```bash
.venv/bin/python scripts/watch_correction.py \
  --work-dir "$HOME/haixia-correct-work" --notify-run "$RUN_ID" --interval-min 2

# 不需 Orca 通知時，省略 --notify-run 即可。
.venv/bin/python scripts/watch_correction.py \
  --work-dir "$HOME/haixia-correct-work" --interval-min 2
```

預設每 10 分鐘檢查一次；帳號接力時建議 `--interval-min 2`，額度用完的通知才夠即時。另可用 `--min-free-gb` 設磁碟警戒值、`--orca-bin` 指定 Orca 執行檔。每天 9 點後首次檢查會送進度摘要；用 `--daily-status-hour -1` 關閉。執行結束或手動中止時，監視程式會送最後狀態並自行結束。

送到 Orca 的訊息一律用 `status` 類型：監視程式不是 Orca worker，送 `escalation` 會被拒收（`sender_not_assignee`）。所以用標題區分：警報是「校正監視警報：<類型>」（例如「校正監視警報：Antigravity 額度用完，請切換 Gemini 帳號」），每日進度是「校正每日進度」，結束時是「校正執行結束」或「校正已手動中止」。

## Antigravity 額度用完時換 Gemini 帳號接力

Antigravity 有 5 小時額度和**週額度**（`agy` 互動模式的 `/usage` 可以看剩多少）。使用者有多個 Gemini 帳號，任何一次額度用完（不論 5 小時或週額度）都可以換下一個帳號接力。全量校正建議加 `--quota-poll-min 10`，監視程式用 `--interval-min 2`：

```bash
caffeinate -i .venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --engines agy --agy-jobs 4 --quota-poll-min 10
```

流程：

1. 監視程式發「Antigravity 額度用完，請切換 Gemini 帳號」通知，內容寫明是 5 小時額度還是週額度、預計何時重設，以及目前登入的帳號（`agy_account`）。同一輪額度用完只通知一次；agy 恢復並成功完成一次呼叫後，下一次用完才算新的一輪。
2. 在 `agy` 互動模式輸入 `/logout`，再登入另一個 Gemini 帳號。其他開著的 agy 互動視窗要關掉或登入同一個帳號，否則它們更新登入資料時會把帳號改回去（見下一節）。
3. 如果有設定預期帳號，把新帳號寫進 `expected-account`（見下一節），例如 `echo c@example.com > "$HOME/haixia-correct-work/expected-account"`。
4. `touch "$HOME/haixia-correct-work/resume-now"`：校正程式立刻解除額度暫停、試探一次，並刪除這個檔（log 會印「收到 resume-now，立即重試」）。不 touch 的話，最多等 `--quota-poll-min` 分鐘也會自動接上。

有 `--quota-poll-min N` 時，Antigravity 因額度暫停最多只停 N 分鐘；到時先只讓**一個** worker 試探呼叫，其餘 worker 等結果，避免 4 個 worker 同時再打出 429。試探成功就解除暫停、全部 worker 恢復；仍是額度錯誤就再暫停最多 N 分鐘。試探開始前就送出的舊呼叫（例如換帳號前送出、晚到的 429）不再造成暫停，該段直接重試。`resume-now` 不論有沒有 `--quota-poll-min` 都有效；沒有額度暫停時收到這個檔，只會刪除並記一行 log。

不加 `--quota-poll-min` 時行為與以前相同：額度錯誤有合理的 `Resets in`（6 小時以內）就暫停到重設後 2 分鐘；週額度的倒數是幾十個小時，不採用，改為指數退避，最長每 60 分鐘重試一次，這時也可以用 `resume-now` 立刻接上。

`status.json` 另記錄這一輪額度用完的資訊，agy 成功完成一次呼叫後清成 `null`：

- `agy_quota_exhausted_since`：這一輪額度用完的開始時間，監視程式用它辨識同一輪；
- `agy_quota_message`：最近一次額度錯誤的 `short_error`（金鑰已遮蔽，含 `Resets in`）；
- `agy_quota_kind`：重設倒數超過 6 小時是 `weekly`，否則是 `five_hour`；
- `agy_quota_resets_at`：依最近一次錯誤的倒數推算的重設時間（週額度也算）。

`engines.agy` 另有 `probing`（是否正在試探）和 `quota_poll_min`。Codex 的額度暫停或停用不會發換帳號通知，Codex 達週上限只記 INFO。舊版校正程式寫的狀態檔沒有 `agy_quota_exhausted_since`，監視程式遇到時沿用舊的判斷：額度暫停記 INFO，週額度用完才發「Antigravity 週額度用完」警報，6 小時內只送一次。

## Antigravity 實際登入的帳號與帳號不符的暫停

### 根本原因：所有 agy 程序共用同一份登入資料

- 所有 agy 程序共用 macOS 鑰匙圈裡的同一份登入資料（agy 的 log 裡是 `keyringAuth`）。
- 長時間開著的互動式 agy 大約每小時自動更新登入資料（log：`token refreshed, new expiry=…`），並寫回鑰匙圈。
- 所以在一個 agy 視窗切到另一個帳號之後，只要另一個還登入原帳號的 agy 視窗更新登入資料，共用的登入資料就變回原帳號。
- 校正程式每一段都開新的 agy 程序，啟動時從鑰匙圈讀登入資料，於是會**不知不覺改用別的帳號的額度**。2026-09-26 發生過 4 次。

所以換帳號時，其他開著的 agy 互動視窗要關掉或登入同一個帳號。校正程式另外逐次記錄實際用的帳號，帳號不對就暫停。

### 記錄每次呼叫實際登入的帳號

- 每次 agy 呼叫都加 `--log-file <work-dir>/agy-logs/<時間>-call-<執行緒>.log`。`--log-file` 是根層級旗標，要放在 `-p` 或子命令前面：`agy --log-file X models` 可以，`agy models --log-file X` 會回「flags provided but not defined」。
- 每個 agy 程序的 log 都有一行 `applyAuthResult: email=<帳號>, authMethod=…`。呼叫結束後，程式從 log 取出帳號，寫進該次嘗試的快取（`chunks/…/NNNNN.attemptK.json` 的 `agy_account`）。解析不到帳號時記 `null`，這一段照常處理，不算失敗。
- `agy-logs/` 只保留最新 500 個檔，舊的自動刪除。這些 log 含帳號，只放在 repo 外的工作目錄。
- `status.json` 的 `agy_account` 是最後一次看到的帳號，以最晚送出的呼叫或檢查為準。

### 預期帳號

有兩種設定方式，兩者都有時以檔案為準：

- `--expected-agy-account a@example.com`；
- `<work-dir>/expected-account` 檔，內容是帳號（只取第一個非空白字串）。**每次檢查都重讀這個檔**，改了不必重啟校正程式。程式發現檔案改了會記一行「預期的 Antigravity 帳號改為 …」，並立刻用 `agy models` 檢查一次。

```bash
echo a@example.com > "$HOME/haixia-correct-work/expected-account"
```

帳號比對不分大小寫。兩者都沒有設定時只記錄 `agy_account`、不管控，其他行為和以前相同。

### 帳號不符時

- 某次 agy 呼叫的實際帳號和預期不同時，**立刻暫停 Antigravity**，不再派新的 agy 呼叫。已經在跑的呼叫照常跑完。
- 發現不符的那次呼叫，結果照常驗收、照常採用：品質沒問題，只是用了別的帳號的額度。
- 暫停期間每 2 分鐘用 `agy --log-file … models` 檢查目前登入的帳號。這不耗模型額度。符合預期就解除暫停、記一行「Antigravity 帳號已符合預期（…），解除帳號不符的暫停，繼續派送」，然後繼續跑。
- `touch <work-dir>/resume-now` 會立即檢查一次，改了 `expected-account` 也會。
- 帳號恢復之前就送出的呼叫晚到、回報舊帳號時，不會再次暫停。
- 帳號不符的暫停和額度暫停互相獨立：
  - 帳號對了但沒額度，照原本的額度邏輯處理（`--quota-poll-min`、試探）；
  - 額度暫停到期但帳號仍不符，繼續暫停。
- 雙引擎接力（`--codex-mode relay`）時，帳號不符的暫停也讓 Codex 接手；Antigravity 恢復後，Codex 照原本的規則停止取新段。
- **啟動時**：有設定預期帳號時，程式啟動時本來就會跑 `agy models` 確認模型，這時一併確認帳號。不符就直接進入帳號不符的暫停，log 會印出實際帳號、預期帳號和處理方式。

`status.json` 的欄位：

- `agy_account`：最後一次看到的實際帳號；
- `agy_expected_account`：目前的預期帳號，沒設定時是 `null`；
- `agy_account_mismatch`：`true`／`false`；
- `agy_account_mismatch_since`：這一輪帳號不符的開始時間，監視程式用它辨識同一輪。

暫停期間 `engines.agy.state` 是 `paused`。只用 Antigravity 時，整體 `state` 也是 `paused`，但 `paused_until` 是 `null`，因為要等帳號對了才恢復，沒有預定時間。

### 監視程式的通知

- `agy_account_mismatch` 從 false 變 true 時，送一次「校正監視警報：Antigravity 帳號不符」。同一輪（`agy_account_mismatch_since` 相同）只送一次；帳號對了之後再不符，算新的一輪。
- 通知內容有預期帳號、實際帳號和處理方式：請在 agy 視窗登入預期的帳號；如果要改用實際的帳號，請告訴指揮更新 `expected-account`。
- 帳號恢復時，只在 `logs/watch.log` 記一行 INFO，不送通知。
- 額度用完的警報也會寫出目前帳號（`agy_account`）。

收到帳號不符的通知時，有兩種處理方式：

1. 要繼續用預期的帳號：在 agy 視窗登入預期的帳號，並關掉其他登入別的帳號的 agy 視窗。校正程式 2 分鐘內會自動接上，也可以 `touch resume-now` 立即檢查。
2. 要改用實際的帳號：由指揮更新 `expected-account`（例如 `echo b@example.com > "$HOME/haixia-correct-work/expected-account"`）。程式會立刻檢查，符合就繼續。

## 驗收

先查看摘要裡的 `failed`、`partial`、平均搜尋次數和實際速度，再抽查校正版的 `correction.chunks`。每段的 `fallback_lines` 是沿用 ASR 的行數。`failed` 段全部沿用 ASR；`partial` 段的未對齊或異常行沿用 ASR。每個 segment 的 `text`、`text_asr` 可逐行比對，`corrected` 表示該行是否取自校正結果。可用下列指令檢查所有輸出格式：

```bash
.venv/bin/python -c 'import json, pathlib, sys; from haixia.transcript import validate_corrected; [validate_corrected(json.loads(p.read_text(encoding="utf-8"))) for p in pathlib.Path(sys.argv[1]).rglob("*.json")]; print("格式檢查通過")' "$HOME/haixia-corrected"
```

同步腳本只用 `rclone copy` 和 `rclone check --one-way`，不會刪除 GCS 物件。推送前先確認抽查結果。
