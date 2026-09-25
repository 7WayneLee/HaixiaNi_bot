# 第三步：校正逐字稿

這一步在使用者的 Mac 上執行。輸入是 `haixia.transcript/1` 的 Whisper ASR JSON；輸出是 `haixia.corrected/1`，保留每段原文 `text_asr`、校正標記與搜尋／耗時紀錄。原始 ASR 不會被覆寫。

## 前置條件

- 安裝並登入 Antigravity CLI；`agy models` 應列出 `gemini-3.8-flash-high`。
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

# 查看品質和額度後，依課程資料夾分批跑全量；已完成的輸出會跳過。
caffeinate -i .venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --jobs 4 \
  --include '影片/05 傷寒論/'

# 檢查後只重跑 failed／partial 段，其餘段沿用快取或既有校正版。
caffeinate -i .venv/bin/python scripts/correct_transcripts.py \
  --in-dir "$HOME/haixia-asr" --out-dir "$HOME/haixia-corrected" \
  --work-dir "$HOME/haixia-correct-work" --jobs 4 --retry-failed \
  --include '影片/05 傷寒論/'

scripts/sync_transcripts.sh push "$HOME/haixia-corrected"
```

`pull` 可以加 `--include '影片/02 針灸/'`，只傳回指定來源前綴；CLI 也可重複加 `--include`，或用 `--files` 指定相對 JSON 路徑清單（每行一個）。全量校正建議依課程資料夾分批執行，降低 Mac 記憶體用量，也方便逐批驗收。`--limit-hours` 依檔名排序累加整檔音訊時數，到達門檻即停。`--retry-failed` 只重跑既有校正版中 `failed` 或 `partial` 的段；`--force` 會重做所有已輸出段。規則、詞表或提示詞改變時，尚未輸出的檔會自動捨棄舊段落快取，已輸出的檔需加 `--force` 重做。

## 暫停、續跑與監看

每段、每次嘗試會存到 `工作目錄/chunks/`，完成檔則寫到 `--out-dir`。按 Ctrl-C 後，程式立即停止派新段，向進行中的 `agy` 程序群組送出 SIGTERM；5 秒後仍未結束就送 SIGKILL。被中止的段不寫入快取，用同一組目錄重跑即可續跑。退出碼 0 表示無 failed 段，1 表示有 failed 段沿用 ASR，2 表示手動中止。

額度用完或被限速時，所有 worker 暫停，從 5 分鐘起指數退避，最多 60 分鐘；這不扣段落重試次數。網路錯誤、空輸出或其他 CLI 錯誤若連續三次出現，也會啟動全體斷路暫停；暫停中其他 worker 回報的錯誤不會提高退避等級。成功呼叫會清除連續錯誤計數。`工作目錄/logs/correct.log` 有每段結果、仍有 failed 段的檔名和每五分鐘進度；`工作目錄/status.json` 有 `running`、`paused`、`finished`、`aborted` 狀態、下次嘗試時間、計數與最後錯誤。

## 驗收

先查看摘要裡的 `failed`、`partial`、平均搜尋次數和實際速度，再抽查校正版的 `correction.chunks`。每段的 `fallback_lines` 是沿用 ASR 的行數。`failed` 段全部沿用 ASR；`partial` 段的未對齊或異常行沿用 ASR。每個 segment 的 `text`、`text_asr` 可逐行比對，`corrected` 表示該行是否取自校正結果。可用下列指令檢查所有輸出格式：

```bash
.venv/bin/python -c 'import json, pathlib, sys; from haixia.transcript import validate_corrected; [validate_corrected(json.loads(p.read_text(encoding="utf-8"))) for p in pathlib.Path(sys.argv[1]).rglob("*.json")]; print("格式檢查通過")' "$HOME/haixia-corrected"
```

同步腳本只用 `rclone copy` 和 `rclone check --one-way`，不會刪除 GCS 物件。推送前先確認抽查結果。
