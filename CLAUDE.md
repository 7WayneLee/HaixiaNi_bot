# CLAUDE.md：倪海廈 Bot

## 專案目的

個人用的倪海廈教學研讀助手。以使用者收集的倪師課程影片與資料（約 100GB，在 Google Drive 的「中醫」資料夾）做檢索式問答（RAG），透過 Telegram 使用。只有使用者本人使用。

## 環境

| 項目 | 狀態 |
|---|---|
| GCP 專案 | `vmdemo1-507014`，已開帳單 |
| 區域 | `us-central1`，所有新資源都放這裡 |
| GPU 配額 | `GPUS_ALL_REGIONS` = 1（已核准）；us-central1 的 `NVIDIA T4 GPUs` 尚未確認 |
| GCS bucket | `haixiani-bot-data-507014`（預定名稱，2026-09-25 確認**還沒建立**）。專案裡另有 `movie-nas-474`（US-WEST1），是別的用途，不要動 |
| Drive 資料 | 使用者 Google Drive 的「中醫」資料夾 |
| 現有 VM | `movie-nas`，`us-west1-b`（不在 us-central1，搬到 bucket 會多約 1–2 美元的跨區流量費），e2-micro（1GB RAM），Debian 12。Mac 上用 `ssh movie-nas` 連線 |
| VM 的 rclone | v1.75.0；remote 叫 `gdrive`（Drive）和 `gcs`（GCS，`env_auth`），跟腳本預設一樣，不用設 `DRIVE_REMOTE`／`GCS_REMOTE`。**Drive 的 token 已過期**（`invalid_grant`，2026-09-25），要先 `rclone config reconnect gdrive:` |
| VM 的 GCS 權限 | access scope 是 `cloud-platform`，可以寫 GCS，不用停機改設定。服務帳號是預設的 Compute Engine 服務帳號 |
| VM 的限制 | 這台 VM 還跑著其他常駐服務（包括用同一個 `gdrive` remote 的 WebDAV），可用記憶體只有約 300MB。搬資料、盤點都要降低並行數，以免 OOM 拖垮其他服務。tmux、ffmpeg、fuse3 還沒裝 |
| GitHub | `7WayneLee/HaixiaNi_bot`（公開 repo）。2026-09-25 時 GitHub 上只有 README.md，腳本還沒 push |

## 已定案的架構

- **RAG，不做微調。**
- **資料處理（一次性）**：Drive →（rclone）→ GCS `raw/` → GPU VM：ffmpeg 抽音訊 → faster-whisper large-v3 轉錄 → OpenCC `s2tw` 轉繁體 → 帶時間戳的逐字稿存回 GCS `transcripts/`
  - 已有字幕檔或內含字幕軌的影片，直接用字幕，不轉錄；重複的檔案只處理一次（依 `manifest.csv`）
  - Whisper 的 `initial_prompt` 放中醫詞彙（方劑、穴位、藥名），提高辨識率
  - 轉錄要能中斷續跑（用 Spot VM），已完成的檔案跳過
- **索引**：切成數百字一段，每段保留課名、集數、起訖時間、檔案路徑；混合檢索＝向量搜尋（開源中文 embedding，例如 bge-m3）＋關鍵字搜尋（BM25）
- **回答**：Claude API（官方 `anthropic` Python SDK）。把「搜尋資料庫」做成 tool，讓 Claude 可以查多次。模型預設 `claude-opus-5`，若要省錢可改 `claude-sonnet-5`，由使用者決定
- **介面**：Telegram bot，用 long polling（不需要網域或 HTTPS），跑在小 VM 上
- **語言**：Python

## 回答規則（寫 system prompt 時要落實）

1. 以倪師的教學為準，每個論點都附出處（課名、集數、時間點）
2. 明確分開【倪師原文依據】和【推論（非倪師原話）】，並標示把握程度
3. 資料裡沒有的病例：先問診（寒熱、汗、口渴、二便、睡眠、飲食、舌象等），再推理
4. 推理只用倪師的框架（六經辨證、經方），不混入其他學派；用到資料以外的一般知識要標示出來
5. 有急重症徵兆時提醒就醫；不自稱是倪海廈本人
6. 資料裡找不到就說找不到，不要編造

## 驗證

用倪師的醫案當測試集：只給症狀、把答案藏起來，比對 bot 推出的證型和方向，記錄準確率和錯誤類型（缺資訊、檢索沒找到、推理錯）。改了 prompt 或檢索設計之後要重跑。

## 進度

- [x] 第一步的腳本：`scripts/transfer.sh`、`scripts/inventory.py`、`docs/01-drive-to-gcs.md`（已寫好，尚未在 VM 上執行）
- [ ] 第一步執行：搬資料、盤點，產生 `manifest.csv`
  - [x] 檢查現有 VM：remote 名稱、GCS 權限、區域（2026-09-25，結果見「環境」）
  - [ ] 重新授權 rclone 的 Drive（token 過期）
  - [ ] 建立 bucket（us-central1）
  - [ ] 腳本 push 到 GitHub，在 VM 上 clone、跑 `setup_worker.sh`
  - [ ] 用 `transfer.sh` 搬「中醫」資料夾
  - [ ] 用 `inventory.py` 盤點，`manifest.csv` 存到 bucket 的 `meta/`
- [ ] 第二步：抽音訊、轉錄
- [ ] 第三步：校對、切段、建索引
- [ ] 第四步：Claude 問答
- [ ] 第五步：Telegram bot
- [ ] 第六步：醫案測試

## 工作守則

- 會花錢或無法復原的 GCP 操作（建立或刪除資源、開 GPU VM、修改 VM 設定、停機），先問使用者
- GPU VM 用完就停機，優先用 Spot
- 機密資訊（Anthropic API key、Telegram token、rclone.conf）放在 VM 上的 `.env`，不要 commit（repo 是公開的）
- 說明文件、腳本訊息、給使用者的回覆都用繁體中文
- 長時間的工作在 tmux 裡跑，並留 log
