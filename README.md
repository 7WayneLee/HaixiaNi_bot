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
| `haixia/textnorm.py` | 簡轉正體（含中醫詞典與台灣用字）與搜尋比對鍵 |
| `data/` | 中醫正體詞表與轉換規則 |
| `tests/` | 測試（`python -m pytest`） |

## 注意

這個 repo 是公開的。API key、Telegram bot token、rclone 設定檔等機密資訊一律放在 VM 上的 `.env`，不要 commit 進來。
