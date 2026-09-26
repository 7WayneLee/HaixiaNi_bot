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
| GPU VM | `haixia-gpu`，us-central1-a，g2-standard-4（1 張 L4，23GB），**一般計費（STANDARD）**，映像檔 `common-cu129-ubuntu-2404-nvidia-580`，200GB 開機磁碟，network tag `haixia-gpu`。2026-09-25 的經過：第一台 Spot 開機 11 分鐘就被收回；改成一般計費後，us-central1-a 的 L4 又整區缺貨；刪掉重建時，b、c 兩區的 L4 和 a、b、c、f 四區的 T4 也都缺貨，最後在 a 區有容量時建成。專案的防火牆預設只允許使用者家裡的 IP 連 SSH，所以另外建了規則 `haixia-gpu-ssh-from-movie-nas`：只允許 movie-nas 內部 IP 連 tcp:22，只套用到有 `haixia-gpu` tag 的 VM。操作方式：在 movie-nas 上 `gcloud compute ssh haixia-gpu --zone us-central1-a --internal-ip`。長時間工作一定要用會回報失敗狀態的監視（VM 狀態、工作程序、log 是否更新、SSH、log 裡新的「失敗」）。**DLVM 映像檔開機約 30 分鐘後會自動跑 unattended-upgrades，連 systemd 都會重新載入、重啟一批服務，把 tmux 裡的工作殺掉（2026-09-25 發生過）**：所以 setup 之後要先 `sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service`，長時間工作要用 `sudo systemd-run --unit=<名稱> --uid=… --working-directory=…` 以 systemd 服務的方式跑，不要放在 SSH 連線底下的 tmux 裡。（已寫進 setup_gpu.sh，長時間工作用 `scripts/gpu_job.sh start <名稱> -- <指令>`）刪 VM 時一併刪掉這條規則 |
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
  - [x] 小規模比較已跑完（2026-09-25，L4 一般計費）：25 份結果在 `gs://haixiani-bot-data-507014/bakeoff/`。速度（RTF）：whisper-prompt 0.078、whisper-noprompt 0.080、whisper-batched 0.029（但每 10 分鐘只切 16–22 段，時間點太粗）、sensevoice 0.031、paraformer 0.054。發現兩種要加進過濾器的幻聽：whisper 夾著「好」的重複迴圈（「你怎麼知道，好，你怎麼知道……」）、batched 把提示詞吐出來（「中文逐字稿：陰陽、表裡……」重複）。「說白傷寒論」確認是梁冬對話郭生白，不轉
  - [x] 使用者校對 15 分鐘參考答案（2026-09-25，存在 `gs://haixiani-bot-data-507014/bakeoff/references/`），評分結果（字錯率／中醫詞召回率）：whisper-noprompt 14.5%／51%、whisper-prompt 15.8%／51%、whisper-batched 21.0%／45%、paraformer 27.0%／59%、sensevoice 30.7%／32%
  - [x] 校正盲測（sol 只拿模型輸出、詞表與使用者的校正原則，看不到答案）：只用 Whisper 校正 → 9.2%／93%；Whisper 加 Paraformer 校正 → 8.8%／98%。Paraformer 帶來的改善太小（字錯率少不到 2 個百分點、召回率多不到 10 個百分點），依使用者決定**不跑 Paraformer**。LLM 校正本身效果很大，但用訂閱額度做全量校正會吃掉好幾週的 Codex 額度，不可行；第三步的校正方式待定（Claude API 批次需要先小量試跑、估算費用）
  - [x] 轉錄引擎定案：**Whisper large-v3，不加提示詞**（不用 Paraformer、SenseVoice、批次模式）
  - [x] 全量抽音訊：影片資料夾 440 個檔轉 16kHz 單聲道 FLAC，放在 `gs://haixiani-bot-data-507014/audio/<raw 相對路徑>.flac`（440 個、23.6 GiB，0 個失敗；2026-09-25 完成後 GPU VM 已停機）
  - [ ] 全量轉錄
- [ ] 第三步：校對、切段、建索引
  - [x] 校正方式試跑（2026-09-25，同樣 4 段、15 分鐘的盲測）：Antigravity CLI（`agy -p`，Gemini 3.8 Flash High，用使用者的訂閱額度，不另外花錢），字錯率／中醫詞召回率：不上網 9.2%／95%（每段 2–3 分鐘）；上網搜尋不限次數 8.7%／96%，但每段搜 29–71 次（大多在找網路上現成的逐字稿），每段要 6–12 分鐘；每段最多搜 5 次 9.2%／94%（每段 2.5–4 分鐘）。4 段同時送沒有變慢，也沒有被限速。使用者要求校正時要能上網查證。agy 的非互動模式沒辦法按「允許」，所以做法是在專用工作目錄的 `.agents/hooks.json` 放 PreToolUse hook：只放行 `search_web`（每段最多 5 次），開網頁、執行指令、讀寫檔案一律拒絕並記錄；不改使用者的 agy 全域設定。測試時 Gemini 被擋掉開網頁之後，還試著改用 Python 程式抓網頁、翻自己的內部檔案，都被 hook 擋下，所以不給它完整權限
  - [x] 使用者決定（2026-09-25）：每段最多搜 5 次；在使用者的 Mac 上跑（agy 已登入；用 `caffeinate` 防止睡眠，中斷可續跑）；先拿已轉好的檔案試跑約 10 小時份量，量速度與額度，再跑全量。校正版放 `transcripts/corrected/`，Whisper 原始版留在 `transcripts/asr/` 不動
  - [x] 校正批次程式（2026-09-25，sol 撰寫，經一輪審查修正，206 個測試通過）：
    - `scripts/correct_transcripts.py`、`haixia/correction.py`、`tools/agy_hook/`、`scripts/sync_transcripts.sh`、`docs/03-correct.md`；輸出 `haixia.corrected/1`（保留 `text_asr`）。
    - 每段約 4 分鐘，附前後各約 60 秒的上下文。逐段驗收，不合格就重跑，驗收不過的結果不採用。
    - 額度用完、網路斷線，或連續 3 次呼叫失敗時，全體暫停並指數退避。
    - `--retry-failed` 只重跑壞段；Ctrl-C 會立刻停止。
    - Mac 沒有 GCS 權限，逐字稿經 movie-nas 中轉。
  - [x] 試跑約 10 小時（2026-09-25 22:52 至 09-26 03:54；針灸 10 檔，含使用者校對過的那一檔；天紀 8 檔）：
    - 152 段全部 ok，0 partial、0 failed、0 行回退；約 4 成的行有改動（例：去蝕→曲池、齒折→尺澤、筆掛→比卦、榴槤掛→流年卦）。
    - 使用者校對過的針灸片段：字錯率 27.5% → 18.7%，中醫詞召回 100%。
    - 平均搜尋 3.5 次；有額度時約每小時 6 小時音訊。
    - **Antigravity 額度是 5 小時一個週期、所有模型共用**，一個週期約能跑 90–100 段，全量約 3,300 段要跑約 7 天；使用者接受維持每段最多搜 5 次，也接受這段期間自己不用 Antigravity。
    - 之後程式改成依錯誤訊息的 `Resets in` 準時恢復，並加上不用 AI 的監視程式 `scripts/watch_correction.py`（出事時用 Orca 通知指揮、每天 9 點送進度）。
    - 監視用的 AI worker 改用 `gpt-6-luna`（medium）：2026-09-26 01:13 三個 gpt-6-sol（xhigh）worker 把 Codex 額度用完，監視中斷。
  - [x] 加入 Codex 引擎（2026-09-26）：
    - 同樣 4 段盲測：Codex gpt-6-sol（medium）字錯率 9.6%、召回 96%，每段 40–70 秒；gpt-6-luna（medium）11.0%、86%，不採用。
    - 使用者決定：
      - 用 sol（medium）跟 Antigravity **接力**：agy 沒額度時 Codex 接手，agy 恢復後 Codex 停止取新段；
      - Codex 本週額度用到 80% 就停，5 小時額度到 85% 先暫停；
      - 第二個 Google 帳號先不開。
    - `codex exec` 的即時網路搜尋在這台機器有授權錯誤，所以用 cached。
    - 約 2–6 成的呼叫會 websocket 斷線，改用 HTTPS 時又出現 401（Codex 0.157.0 的備用連線用錯憑證），程式會重試。
    - 換成付費 Gemini API 估計要 17,000–21,000 台幣（批次約 10,000），遠超預算，不採用。
  - [ ] 全量校正（2026-09-26 06:37 開始，先跑已轉好的 310 檔；轉錄完成後再補其餘檔案）
    - 07:42 起改成雙引擎接力。Codex 在 25 分鐘內跑了 123 段、全部 ok，但 5 小時額度從 29% 升到 82%、本週從 40% 升到 49%（每段約占週額度 0.08%），所以 08:11 依使用者要求**暫停 Codex**，只用 Antigravity。
    - Antigravity 也有**週額度**（2026-09-26 08:08 剩 33.7%，約 52 小時後重設；`agy` 的 `/usage` 可看）。
    - 使用者決定維持原計畫：不改課程順序，每段最多搜 5 次。使用者有三個 Gemini 訂閱帳號，一個帳號的週額度用完就換下一個接力。
    - 監視程式看到重設倒數超過 6 小時的暫停，會發「Antigravity 週額度用完」警報，指揮要轉告使用者換帳號；換好後按 Ctrl-C 重跑就能立刻接上。
- [ ] 第四步：Claude 問答
- [ ] 第五步：Telegram bot
- [ ] 第六步：醫案測試

## 預算

- 架設整個系統（第一步到第六步）的總預算約 **1,100 台幣**（約 34 美元，以 1 美元 ≈ 32 台幣估算），**不含**之後 bot 問答用的 Claude API 費用。
- 2026-09-25 估算：已花約 97 台幣（跨區傳輸、GPU 約 2 小時）；第二步只跑 Whisper 約 450 台幣，加跑 Paraformer 再多約 290 台幣；GCS 每月約 64 台幣。
- 因為預算緊，有三個原則：(1) 大量的 LLM 校正用訂閱額度（Antigravity 的 Gemini），不走付費 API；(2) GPU VM 不用就刪掉，停機時 200GB 磁碟每天約 21 台幣；(3) 使用者要求 GCS 上保留一份原始資料，所以 `raw/` 不刪（原本打算第二步後刪掉），而且維持標準儲存等級，不轉封存（2026-09-25 使用者決定；每月約 46 台幣）。音訊第三步後再決定。
- 任何會增加花費的新做法，先估算、先問使用者。

## 工作守則

- 會花錢或無法復原的 GCP 操作（建立或刪除資源、開 GPU VM、修改 VM 設定、停機），先問使用者
- GPU VM 用完就停機，優先用 Spot
- 機密資訊（Anthropic API key、Telegram token、rclone.conf）放在 VM 上的 `.env`，不要 commit（repo 是公開的）
- 說明文件、腳本訊息、給使用者的回覆都用繁體中文
- 長時間的工作在 tmux 裡跑，並留 log
