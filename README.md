# HaixiaNi_bot

以倪海廈老師的課程影片與資料為基礎的個人研讀助手。在 Telegram 上提問，回答會附上原文出處（哪一堂課、第幾分鐘）；遇到資料沒有直接提到的情況，會先問診，再依倪師的原則推理，並清楚標示哪些是原文、哪些是推論。

> 僅供個人學習研究，不是醫療建議。

## 架構

```
階段一：一次性的資料處理
  Google Drive ──► GCS ──► GPU VM：抽音訊、Whisper 轉錄 ──► 逐字稿
                                                         │
                                          校對、切段、建搜尋索引
                                                         ▼
階段二：長期運作的 bot
  Telegram ◄──► 小 VM（bot＋搜尋索引）◄──► Claude API
```

所有 GCP 資源都放在 `us-central1`（專案 `vmdemo1-507014`）。

## 進度

- [ ] 第一步：Drive → GCS、盤點資料 → [docs/01-drive-to-gcs.md](docs/01-drive-to-gcs.md)
- [ ] 第二步：抽音訊、Whisper 轉錄（GPU VM）
- [ ] 第三步：校對、切段、建搜尋索引
- [ ] 第四步：Claude 問答（引用出處、問診式推理）
- [ ] 第五步：Telegram bot
- [ ] 第六步：用倪師醫案測試推理準確度

## 目錄

| 路徑 | 用途 |
|---|---|
| `docs/` | 每一步的操作說明 |
| `scripts/setup_worker.sh` | 在 VM 上安裝 rclone、ffmpeg 等工具 |
| `scripts/transfer.sh` | 把 Drive 資料夾複製到 GCS |
| `scripts/inventory.py` | 盤點檔案類型、大小與影音時數 |
| `scripts/sample_frames.py` | 每門課抽幾部影片截圖，拼成總覽圖，檢查有沒有燒進畫面的字幕 |
| `scripts/extract_audio.py` | 影音轉成 16kHz 單聲道 FLAC（可切片段、可批次續跑） |
| `scripts/transcribe.py` | 轉錄：Whisper large-v3、SenseVoice、SeACo-Paraformer，輸出帶時間與信心分數的逐字稿 JSON |
| `scripts/lrc_to_transcript.py` | 把現成的 `.lrc` 字幕轉成同格式的逐字稿 |
| `scripts/run_bakeoff.sh`、`scripts/bakeoff_score.py` | 小規模比較：跑 5 種設定、算字錯率與中醫詞召回率 |
| `scripts/create_gpu_vm.sh`、`scripts/setup_gpu.sh` | 建立 L4 GPU VM（預設一般計費、只預覽）、安裝轉錄環境並關掉自動更新 |
| `scripts/gpu_job.sh` | 在 GPU VM 上以 systemd 服務跑長時間工作（start／status／log／stop） |
| `scripts/correct_transcripts.py` | 在 Mac 上用 Antigravity CLI 批次校正逐字稿，可續跑與監看狀態 |
| `scripts/watch_correction.py` | 不用 AI 監視校正作業，異常時寫警報並可通知 Orca Run |
| `scripts/sync_transcripts.sh` | 經由 movie-nas 複製 ASR 與校正版逐字稿；操作見 [docs/03-correct.md](docs/03-correct.md) |
| `haixia/correction.py`、`tools/agy_hook/` | 校正核心邏輯、提示詞與工具閘門 |
| `haixia/transcript.py` | 逐字稿格式、驗證、幻聽過濾 |
| `haixia/textnorm.py` | 簡轉正體（含中醫詞典與台灣用字）與搜尋比對鍵 |
| `data/` | 中醫正體詞表與轉換規則 |
| `tests/` | 測試（`python -m pytest`） |

## 注意

這個 repo 是公開的。API key、Telegram bot token、rclone 設定檔等機密資訊一律放在 VM 上的 `.env`，不要 commit 進來。
