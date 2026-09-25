# CLAUDE.md：倪海廈 Bot

## 專案目的

個人用的倪海廈教學研讀助手。以使用者收集的倪師課程影片與資料（約 100GB，在 Google Drive 的「中醫」資料夾）做檢索式問答（RAG），透過 Telegram 使用。只有使用者本人使用。

## 環境

| 項目 | 狀態 |
|---|---|
| GCP 專案 | `vmdemo1-507014`，已開帳單 |
| 區域 | `us-central1`，所有新資源都放這裡 |
| GPU 配額 | `GPUS_ALL_REGIONS` = 1。us-central1（2026-09-25 確認）：T4、L4 的一般與 Spot（PREEMPTIBLE）配額各 1；`PREEMPTIBLE_CPUS` = 0（Spot VM 應會改用一般 CPU 配額 200，建立時再確認） |
| GCS bucket | `gs://haixiani-bot-data-507014`，US-CENTRAL1、STANDARD、uniform bucket-level access（2026-09-25 建立）。原始資料在 `raw/`，盤點明細放 `meta/`。專案裡另有 `movie-nas-474`（US-WEST1），是別的用途，不要動 |
| Drive 資料 | 「我的雲端硬碟」的「中醫」資料夾（不在「與我共用」）。2026-09-25 統計：影片 440 個檔案 65.4 GiB、文字資料 1,607 個 6.1 GiB、電子書 11 個 38 MiB，合計約 71.5 GiB；沒有同名重複檔，也沒有 Google 文件格式 |
| 現有 VM | `movie-nas`，`us-west1-b`（不在 us-central1，搬到 bucket 會多約 1–2 美元的跨區流量費），e2-micro（1GB RAM），Debian 12。Mac 上用 `ssh movie-nas` 連線 |
| VM 的 rclone | v1.75.0；remote 叫 `gdrive`（Drive）和 `gcs`（GCS，`env_auth`），跟腳本預設一樣，不用設 `DRIVE_REMOTE`／`GCS_REMOTE`。Drive 的 token 在 2026-09-25 過期過，使用者已重新授權 |
| VM 的 GCS 權限 | access scope 是 `cloud-platform`，可以寫 GCS，不用停機改設定。服務帳號是預設的 Compute Engine 服務帳號 |
| VM 的限制 | 這台 VM 還跑著其他常駐服務（包括用同一個 `gdrive` remote 的 WebDAV），可用記憶體只有約 300MB。第一步搬資料時暫停過這些服務，2026-09-25 第一步完成後已全部恢復（指令記在 VM 的 `~/haixia-stopped-services.txt`）。之後的重工作都在 GPU VM 上跑，不要再佔用這台。repo clone 在 VM 的 `~/HaixiaNi_bot`，`setup_worker.sh` 已跑過（tmux、ffmpeg、fuse3 已安裝） |
| GitHub | `7WayneLee/HaixiaNi_bot`（公開 repo）。第一步的腳本與文件已 push（2026-09-25） |

## 已定案的架構

- **RAG，不做微調。**
- **資料處理（一次性）**：Drive →（rclone）→ GCS `raw/` → GPU VM：ffmpeg 抽音訊 → faster-whisper large-v3 轉錄 → OpenCC `s2tw` 轉繁體 → 帶時間戳的逐字稿存回 GCS `transcripts/`
  - 已有字幕檔或內含字幕軌的影片，直接用字幕，不轉錄；重複的檔案只處理一次（依 `manifest.csv`）
  - Whisper 的 `initial_prompt` 放中醫詞彙（方劑、穴位、藥名），提高辨識率
  - 轉錄要能中斷續跑（用 Spot VM），已完成的檔案跳過
- **正體中文**：使用者一律用正體中文問答。Drive 的文字資料多半是簡體，建索引前統一用 OpenCC `s2tw` 轉成正體（不用 `s2twp`，因為它會改寫用語），再用自建的中醫詞典修正 OpenCC 的錯誤；簡體原檔留在 `raw/`。2026-09-25 實測的錯誤：太冲→太沖（應為太衝）、谷芽→谷芽（應為穀芽）、半表半里未轉、通里／建里（穴位）被轉成裡、湿→溼。使用者決定的顯示用字：**濕、黃耆、痺、穴位一律用溪**（異體字在搜尋時要視為相同）。實作在 `haixia/textnorm.py`：`to_traditional` 的流程是簡體詞典 `data/tcm_s2tw.txt`（最長匹配）→ OpenCC `s2tw` → 正體端詞組修正 `data/tw_phrase_fixes.txt`（處理單獨的姜→薑、加术→加朮等，並保留手術、姜春華等例外）→ 台灣用字 `data/tw_variants.txt`；`search_key` 的流程是 NFKC → 台灣用字 → OpenCC `t2s`，只用於比對。`data/tcm_terms_tw.txt` 是人工校過的正體詞表（穴位 361、方劑、藥名、術語），測試以它為準
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

## 資料盤點（2026-09-25）

- 2,058 個檔案、71.5 GB。影音 569 個：影片 440（rmvb 418、avi 22）、音訊 129（wma 70、mp3 59）。盤點算出 536.5 小時，但黃帝內經 MP3 有 4 個檔被 ffprobe 誤判為 8 kbps（長度多算約 72 小時），實際約 465 小時
- 影音檔依 MD5 沒有任何重複；「大小相同」的 9 組其實是不同內容（固定位元率的廣播）。文件有大量 MD5 重複（多出 492 個 doc、14 個 PDF）
- 影片都沒有內嵌字幕軌，也沒有燒進畫面的字幕（看過 `meta/frames/` 的截圖），只能靠語音轉錄；傷寒、金匱的黑板上有投影的條文
- 「MP3 人紀全」（約 167 小時）和人紀影片是同一批課，只轉影片（影片分集，引用出處較清楚）
- 「梁冬對話倪海廈」7 集（5.9 小時）有完整的 `.lrc` 逐字稿，不必轉錄
- 「國學堂」其他資料夾（約 72 小時）多半不是倪師：劉力紅、蕭啟宏；《黃帝內經》錄音應是梁冬對話徐文兵；「說白傷寒論」講者待確認
- 文字資料：《人紀》講義 PDF 5 本（文字檔，約 1,460 頁）、醫案與診療日誌約 1,400 個 doc、CHM 1 個、RAR 2 個待解壓；天紀《天機道》是掃描檔；藥材照片 zip 是圖片

## 進度

- [x] 第一步的腳本：`scripts/transfer.sh`、`scripts/inventory.py`、`docs/01-drive-to-gcs.md`（已寫好，尚未在 VM 上執行）
- [x] 第一步執行：搬資料、盤點，產生 `manifest.csv`（2026-09-25 完成）
  - [x] 檢查現有 VM：remote 名稱、GCS 權限、區域（2026-09-25，結果見「環境」）
  - [x] 重新授權 rclone 的 Drive（token 過期），已確認看得到「中醫」
  - [x] 建立 bucket（us-central1），寫入測試通過
  - [x] 腳本 push 到 GitHub，在 VM 上 clone、跑 `setup_worker.sh`
  - [x] 用 `transfer.sh` 搬「中醫」資料夾到 `gs://haixiani-bot-data-507014/raw/`（2026-09-25，2 小時 46 分；2,058 個檔案、76,777,590,922 bytes，`rclone check` 逐檔 MD5 全部相符）
  - [x] 用 `inventory.py` 盤點，`manifest.csv` 存到 bucket 的 `meta/`（另存 `md5sum.txt`、`inventory.log`、影片截圖總覽 `frames/`），結果見「資料盤點」
- [ ] 第二步：抽音訊、轉錄
  - [x] 準備：簡轉正體模組與中醫詞表（`haixia/textnorm.py`，sol 撰寫，經三輪審查，117 個測試通過，2026-09-25）
  - [x] 準備：影片截圖總覽 `scripts/sample_frames.py`，用來判斷畫面上有沒有燒進去的字幕（sol 撰寫，已審查）
  - [x] 計畫已確認（2026-09-25）：轉錄人紀影片、八綱辨證、臨牀案例、天紀、六壬，共 220.9 小時；「MP3 人紀全」與人紀影片重複，不轉；梁冬對話倪海廈用現成 `.lrc`；國學堂其他非倪師內容不轉、不進索引；GPU 用 us-central1 的 L4 Spot
  - [x] 第二步的程式（2026-09-25，程式由 sol 撰寫；最後一輪修正時 Codex 額度用完、改由 Claude 直接審查驗收）：`scripts/extract_audio.py`、`scripts/transcribe.py`、`scripts/lrc_to_transcript.py`、`scripts/run_bakeoff.sh`、`scripts/bakeoff_score.py`、`scripts/create_gpu_vm.sh`、`scripts/setup_gpu.sh`、`haixia/transcript.py`、`docs/02-transcribe.md`；163 個測試通過。模型只從官方來源下載（ModelScope `iic/…`，或 Hugging Face 的 FunAudioLLM、funasr 官方倉庫），不用社群鏡像
  - [ ] 小規模比較：4 段各 10 分鐘，比較 Whisper large-v3（有／無提示詞）、SenseVoice、SeACo-Paraformer（熱詞）；使用者校對 15 分鐘當標準答案，比字錯率與中醫詞正確率，並實測速度
  - [ ] 全量轉錄
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
