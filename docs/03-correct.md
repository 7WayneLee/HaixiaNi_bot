# 第三步：校正逐字稿

這一步在使用者的 Mac 上執行。輸入是 `haixia.transcript/1` 的 Whisper ASR JSON；輸出是 `haixia.corrected/1`，保留每段原文 `text_asr`、校正標記與搜尋／耗時紀錄。原始 ASR 不會被覆寫。

## 前置條件

- 安裝並登入 Antigravity CLI；`agy models` 應列出 `gemini-3.8-flash-high`。
- 若使用 Codex，安裝並登入 Codex CLI，並確認 `orca account list --json` 能讀取 Codex 的 session 與 weekly 額度。
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

Codex 在 repo 外的 `工作目錄/codex-ws/` 空資料夾執行，stdin 關閉，網路搜尋設為 `cached`；這台機器的 `live` 搜尋會遇到授權錯誤。Codex 每次呼叫前檢查 Orca 額度，最多每 60 秒查一次。週額度達 `--codex-weekly-max`（預設 80%）時，本次執行停用 Codex，Antigravity 繼續；session 達 `--codex-session-max`（預設 85%）時，Codex 暫停到 `resetsAt` 後 2 分鐘。Codex 自己回報 usage limit 或 HTTP 429 也按 session 重設時間暫停；資料讀不到時保守暫停 15 分鐘。websocket 斷線後接 401 的錯誤視為暫時性連線錯誤重試，並非額度用完。錯誤訊息內的 `sk-` 金鑰字串會在 log、狀態與快取中遮蔽。

## 暫停、續跑與監看

每段、每次嘗試會存到 `工作目錄/chunks/`，完成檔則寫到 `--out-dir`。按 Ctrl-C 後，程式立即停止派新段，向進行中的 CLI 程序群組送出 SIGTERM；5 秒後仍未結束就送 SIGKILL。被中止的段不寫入快取，用同一組目錄重跑即可續跑。退出碼 0 表示無 failed 段，1 表示有 failed 段沿用 ASR 或段落未完成，2 表示手動中止。

Antigravity 的額度每 5 小時重設一次，所有模型共用。額度錯誤若提供 `Resets in` 倒數，Antigravity worker 會暫停至預計重設時間再加 2 分鐘（加了 `--quota-poll-min` 時最多只停那麼久，見下方「換 Gemini 帳號接力」）；多個 worker 回報不同時間時取最晚的。倒數缺失或不合理時，才從 5 分鐘起指數退避，最多 60 分鐘。額度暫停不扣段落重試次數。網路錯誤、空輸出或其他 CLI 錯誤若連續三次出現，也會啟動該引擎的斷路暫停；暫停中其他 worker 回報的錯誤不會提高退避等級。成功呼叫會清除連續錯誤計數。`工作目錄/logs/correct.log` 有每段結果、仍有 failed 段的檔名和每五分鐘進度；`工作目錄/status.json` 有 `running`、`paused`、`finished`、`aborted` 狀態、PID、啟動時間、最後完成段落時間、已知額度重設時間、下次嘗試時間、計數與最後錯誤。

雙引擎模式下，額度暫停、斷路器和連線錯誤各自計算，不會暫停另一個引擎。`status.json` 的 `engines` 區塊記錄各引擎狀態、暫停時間、原因、完成段數與平均耗時；Codex 另記最近的 session／weekly 使用百分比。`codex_mode` 和 `active_engines` 顯示接力／並行模式及目前正在呼叫的引擎。監視程式對非額度原因停止的引擎發警報；Codex 達週上限僅記 INFO。

建議在**另一個 Orca 終端機分頁**啟動不用 AI 的監視程式，檢查 PID、進度、log 與磁碟。它只讀校正資料，僅寫自己的 `logs/watch.log`；異常才發 ALERT。可選擇將警報及每日摘要送到 Orca Run；先把 `RUN_ID` 設成目標 Run ID：

```bash
.venv/bin/python scripts/watch_correction.py \
  --work-dir "$HOME/haixia-correct-work" --notify-run "$RUN_ID" --interval-min 2

# 不需 Orca 通知時，省略 --notify-run 即可。
.venv/bin/python scripts/watch_correction.py \
  --work-dir "$HOME/haixia-correct-work" --interval-min 2
```

預設每 10 分鐘檢查一次；帳號接力時建議 `--interval-min 2`，額度用完的通知才夠即時。另可用 `--min-free-gb` 設磁碟警戒值、`--orca-bin` 指定 Orca 執行檔。每天 9 點後首次檢查會送進度摘要；用 `--daily-status-hour -1` 關閉。執行結束或手動中止時，監視程式會送最後狀態並自行結束。

## Antigravity 額度用完時換 Gemini 帳號接力

Antigravity 有 5 小時額度和**週額度**（`agy` 互動模式的 `/usage` 可以看剩多少）。使用者有多個 Gemini 帳號，任何一次額度用完（不論 5 小時或週額度）都可以換下一個帳號接力。全量校正建議加 `--quota-poll-min 10`，監視程式用 `--interval-min 2`：

```bash
caffeinate -i .venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --engines agy --agy-jobs 4 --quota-poll-min 10
```

流程：

1. 監視程式發「Antigravity 額度用完，請切換 Gemini 帳號」通知，內容寫明是 5 小時額度還是週額度、預計何時重設。同一輪額度用完只通知一次；agy 恢復並成功完成一次呼叫後，下一次用完才算新的一輪。
2. 在 `agy` 互動模式輸入 `/logout`，再登入另一個 Gemini 帳號。
3. `touch "$HOME/haixia-correct-work/resume-now"`：校正程式立刻解除額度暫停、試探一次，並刪除這個檔（log 會印「收到 resume-now，立即重試」）。不 touch 的話，最多等 `--quota-poll-min` 分鐘也會自動接上。

有 `--quota-poll-min N` 時，Antigravity 因額度暫停最多只停 N 分鐘；到時先只讓**一個** worker 試探呼叫，其餘 worker 等結果，避免 4 個 worker 同時再打出 429。試探成功就解除暫停、全部 worker 恢復；仍是額度錯誤就再暫停最多 N 分鐘。試探開始前就送出的舊呼叫（例如換帳號前送出、晚到的 429）不再造成暫停，該段直接重試。`resume-now` 不論有沒有 `--quota-poll-min` 都有效；沒有額度暫停時收到這個檔，只會刪除並記一行 log。

不加 `--quota-poll-min` 時行為與以前相同：額度錯誤有合理的 `Resets in`（6 小時以內）就暫停到重設後 2 分鐘；週額度的倒數是幾十個小時，不採用，改為指數退避，最長每 60 分鐘重試一次，這時也可以用 `resume-now` 立刻接上。

`status.json` 另記錄這一輪額度用完的資訊，agy 成功完成一次呼叫後清成 `null`：

- `agy_quota_exhausted_since`：這一輪額度用完的開始時間，監視程式用它辨識同一輪；
- `agy_quota_message`：最近一次額度錯誤的 `short_error`（金鑰已遮蔽，含 `Resets in`）；
- `agy_quota_kind`：重設倒數超過 6 小時是 `weekly`，否則是 `five_hour`；
- `agy_quota_resets_at`：依最近一次錯誤的倒數推算的重設時間（週額度也算）。

`engines.agy` 另有 `probing`（是否正在試探）和 `quota_poll_min`。Codex 的額度暫停或停用不會發換帳號通知，Codex 達週上限只記 INFO。舊版校正程式寫的狀態檔沒有 `agy_quota_exhausted_since`，監視程式遇到時沿用舊的判斷：額度暫停記 INFO，週額度用完才發「Antigravity 週額度用完」警報，6 小時內只送一次。

## 驗收

先查看摘要裡的 `failed`、`partial`、平均搜尋次數和實際速度，再抽查校正版的 `correction.chunks`。每段的 `fallback_lines` 是沿用 ASR 的行數。`failed` 段全部沿用 ASR；`partial` 段的未對齊或異常行沿用 ASR。每個 segment 的 `text`、`text_asr` 可逐行比對，`corrected` 表示該行是否取自校正結果。可用下列指令檢查所有輸出格式：

```bash
.venv/bin/python -c 'import json, pathlib, sys; from haixia.transcript import validate_corrected; [validate_corrected(json.loads(p.read_text(encoding="utf-8"))) for p in pathlib.Path(sys.argv[1]).rglob("*.json")]; print("格式檢查通過")' "$HOME/haixia-corrected"
```

同步腳本只用 `rclone copy` 和 `rclone check --one-way`，不會刪除 GCS 物件。推送前先確認抽查結果。
