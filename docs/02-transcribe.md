# 第二步：抽音訊、比較轉錄引擎，再全量轉錄

目標：先用四段各 10 分鐘的影音比較五種轉錄設定，校對其中 15 分鐘作為參考答案；選定設定後處理已確認的倪師課程。原始資料保留在 `raw/`，音訊與逐字稿分別存入 bucket 的 `audio/`、`transcripts/`。

```
GCS raw/ ──rclone 唯讀掛載──► GPU VM ──extract_audio.py──► audio/
                                    │                       │
                                    └─ transcribe.py ◄──────┘ ──► transcripts/
```

## 1. 費用與守則

GPU VM 使用 us-central1 的 L4，預設一般計費（STANDARD），取捨見第 2 節。執行時 GPU、CPU 和磁碟計費，停機後開機磁碟仍計費。建立、啟動、停止或刪除雲端資源前，先依 `CLAUDE.md` 的工作守則取得使用者同意。用完當天就停機或刪除，不要讓 GPU 閒置。

## 2. 建立 GPU VM

**在 movie-nas 上**執行；這台 VM 已有 gcloud，Mac 不用安裝。先預覽，再明確建立：

```bash
ssh movie-nas
cd ~/HaixiaNi_bot
bash scripts/create_gpu_vm.sh
bash scripts/create_gpu_vm.sh --yes
```

預設機型為 `g2-standard-4`、200GB 平衡型開機磁碟，映像檔為 CUDA 12.9 / Ubuntu 24.04 的 NVIDIA DLVM，並帶 network tag `haixia-gpu`。`--yes` 會依序：

1. 確認沒有同名 VM。已經有的話停止，改用 `--start --yes`，不會另建第二台。
2. 確認防火牆規則 `haixia-gpu-ssh-from-movie-nas` 存在。專案的防火牆預設只允許使用者家裡的 IP 連 SSH，這條規則只放行 movie-nas 的內部 IP（`10.138.0.2/32`）連 tcp:22，而且只套用到帶 `haixia-gpu` tag 的 VM。規則不存在時，腳本會停止並印出建立它的完整指令，不會自動建立；建立前先問使用者。
3. 查詢 us-central1 提供 nvidia-l4 的 zone，逐一嘗試建立。遇到缺貨（`ZONE_RESOURCE_POOL_EXHAUSTED`）時，若錯誤訊息的 `zonesAvailable` 列出目前有容量的 zone，就把它排到下一個嘗試。每個 zone 最多試一次；全部失敗時列出每個 zone 的原因。缺貨以外的錯誤（例如配額不足）會立即停止。

成功後會印出 `gcloud compute ssh haixia-gpu --zone <zone> --internal-ip`，在 movie-nas 執行該行進入 GPU VM。一定要加 `--internal-ip`：走外部 IP 會被防火牆擋下，只會看到連線逾時。

計費方式用 `PROVISIONING` 選擇，預覽與實際執行都適用：

| `PROVISIONING` | 說明 |
|---|---|
| `STANDARD`（預設） | 一般計費，價格較高，但不會被收回。長時間轉錄用這個。一般計費也會遇到 zone 缺貨（2026-09-25 曾經 b、c 兩區都缺 L4）。 |
| `SPOT` | 較便宜，但可能隨時被收回，收回後 VM 會停止。2026-09-25 實測開機 11 分鐘就被收回。只適合短時間、可以隨時中斷的工作。 |

```bash
PROVISIONING=SPOT bash scripts/create_gpu_vm.sh          # 預覽 Spot
PROVISIONING=SPOT bash scripts/create_gpu_vm.sh --yes
```

## 3. 設定 GPU VM

從 movie-nas 進入 GPU VM（只能走內部 IP，理由見第 2 節），**在 GPU VM 上**執行：

```bash
gcloud compute ssh haixia-gpu --zone <zone> --internal-ip   # 在 movie-nas 上
git clone https://github.com/7WayneLee/HaixiaNi_bot.git
cd ~/HaixiaNi_bot
bash scripts/setup_gpu.sh
source ~/haixia-work/env.sh
```

VM 建好後盡快跑 `setup_gpu.sh`。DLVM 映像檔開機約 30 分鐘後，unattended-upgrades 會自動更新套件，連 systemd 都會重新載入，並重啟一批服務，正在跑的工作會被殺掉（2026-09-25 發生過）。腳本第一步就會關掉 `apt-daily.timer`、`apt-daily-upgrade.timer`、`unattended-upgrades.service`；如果背景已經在更新，會先等它結束（最多 10 分鐘），之後的 `apt-get` 也會等 dpkg 的鎖最多 10 分鐘。

接著腳本會安裝 ffmpeg、rclone、Python 套件、CUDA 版 PyTorch，產生 `~/haixia-work/env.sh`，預先下載 Whisper、SenseVoice、Paraformer、VAD 和標點模型，最後印出 PyTorch 與 CTranslate2 看得到的 CUDA 裝置數。非互動 shell（systemd 服務、`ssh` 直接下指令）不會跑到 `~/.bashrc` 最後那行，所以 cuBLAS/cuDNN 的 `LD_LIBRARY_PATH` 和 `source ~/venv/bin/activate` 都寫在 `env.sh`，互動 shell 也用它。

已 clone 過就進入 `~/HaixiaNi_bot`，更新專案後重跑 `setup_gpu.sh`，可以重複執行。第一次開機的 NVIDIA 驅動可能還在自動安裝；若 `nvidia-smi` 失敗，稍後再執行同一行。

## 4. 掛載 bucket

在 GPU VM 上把 bucket 唯讀掛載，原始檔不會被修改：

```bash
mkdir -p ~/gcs
rclone mount gcs:haixiani-bot-data-507014 ~/gcs --read-only --daemon
rclone copy gcs:haixiani-bot-data-507014/meta/manifest.csv ~/HaixiaNi_bot/
```

重新開機後要重跑 `rclone mount`。掛載目錄下的 `~/gcs/raw` 是以下指令的原始資料根目錄。

## 5. 跑小規模比較

長時間的工作一律用 `scripts/gpu_job.sh` 以 systemd 服務執行，不要放在 SSH 連線底下的 tmux 裡：SSH 斷線、tmux 被殺掉都不影響服務。`gpu_job.sh` 會先 source `~/haixia-work/env.sh`，工作目錄是執行 `start` 時的目錄。

```bash
cd ~/HaixiaNi_bot
bash scripts/gpu_job.sh start bakeoff-run -- bash scripts/run_bakeoff.sh ~/gcs/raw ~/haixia-work
```

`run_bakeoff.sh` 自己會把 log 附加到 `~/haixia-work/bakeoff.log`，所以工作名稱不要取 `bakeoff`，以免兩份 log 寫進同一個檔案。查看狀態與 log：

```bash
bash scripts/gpu_job.sh status bakeoff-run      # 一行狀態，見下表
bash scripts/gpu_job.sh log bakeoff-run 30      # log 最後 30 行
bash scripts/gpu_job.sh stop bakeoff-run        # 需要中斷時
```

| `status` 的輸出 | 意思 |
|---|---|
| `DONE exit=0` | 工作正常結束。`exit` 不是 0 表示失敗，看 log。 |
| `RUN active=active age=12s fail=0` | 還在跑。`age` 是 log 多久沒更新（秒），`fail` 是 log 裡含「失敗」的行數。`age` 一直變大或 `fail` 增加時，看 log。 |
| `RUN active=inactive …` 或 `active=failed` | 沒有寫出結束碼，服務也不在了：被停止、被殺掉或 VM 重開機過。看 log 找原因，再用同一行 `start` 重跑，已完成的結果會跳過。 |

log 在 `~/haixia-work/<名稱>.log`，結束碼在 `~/haixia-work/<名稱>.exit`；重新 `start` 時，舊的 log 會改名為 `<名稱>.log.prev`。看到 `DONE exit=0` 後上傳結果：

```bash
rclone copy ~/haixia-work/results/ gcs:haixiani-bot-data-507014/bakeoff/results/
rclone copy ~/haixia-work/bakeoff.log gcs:haixiani-bot-data-507014/bakeoff/
```

小規模比較的五組結果位於 `~/haixia-work/results/<設定>/<label>.json`。把四個評分區間各自校對成 UTF-8 純文字，存到 `~/haixia-work/references/<label>.txt`：`shanghan`、`zhenjiu`、`bencao` 各 240 秒，`bagang` 180 秒；`speaker` 只確認講者，不評分。每行可用 `[mm:ss]` 或 `[hh:mm:ss]` 起頭，`#` 開頭的行會略過。

## 6. 評分並選擇設定

```bash
cd ~/HaixiaNi_bot
source ~/haixia-work/env.sh
python3 scripts/bakeoff_score.py \
  --clips data/bakeoff_clips.tsv \
  --results ~/haixia-work/results \
  --references ~/haixia-work/references \
  --out ~/haixia-work/bakeoff-report.md \
  --json ~/haixia-work/bakeoff-report.json \
  --terms data/tcm_terms_tw.txt \
  --prompts data/course_prompts.json
rclone copy ~/haixia-work/bakeoff-report.md gcs:haixiani-bot-data-507014/bakeoff/
rclone copy ~/haixia-work/bakeoff-report.json gcs:haixiani-bot-data-507014/bakeoff/
```

報告列出每段的字錯率（CER）、中醫詞召回率、速度（RTF）和低信心段落比例。比對前會把參考答案和轉錄結果都轉成同一套字形，拿掉標點與空白，並預設拿掉純語助詞（嗯、啊、哈、呃、哦、欸、誒、喔、唉、噢），這些字不計入字錯率；加 `--keep-fillers` 可以保留，報告開頭會註明用了哪一種。CER 越低、召回率越高越好；RTF 是處理秒數除以音訊秒數。結果檔缺少時標為「（缺）」，參考答案缺少時標為「（缺參考答案）」；補齊後再決定全量設定。

## 7. 全量抽音訊與轉錄

只處理已確認的倪師課程：人紀影片、八綱辨證、臨床案例、天紀、六壬。`MP3 人紀全` 與影片重複，其他國學堂講者不納入；梁冬對話倪海廈的七集走下一節的 LRC。抽音訊與轉錄都用 `scripts/gpu_job.sh` 以 systemd 服務執行，查看方式同第 5 節；`--include` 可重複指定來源路徑前綴。小規模比較的「設定」和 `transcribe.py` 的引擎參數對照如下：

| 比較設定 | `ENGINE` | 額外參數 |
|---|---|---|
| `whisper-prompt` | `whisper` | 無，預設使用課程提示詞 |
| `whisper-noprompt` | `whisper` | `--no-prompt` |
| `whisper-batched` | `whisper-batched` | 無 |
| `sensevoice` | `sensevoice` | 無 |
| `paraformer` | `paraformer` | 無 |

把下方的 `ENGINE` 設為選定引擎；若選 `whisper-noprompt`，把 `EXTRA_ARGS=()` 改成 `EXTRA_ARGS=(--no-prompt)`。

先抽音訊：

```bash
cd ~/HaixiaNi_bot
bash scripts/gpu_job.sh start extract -- python3 scripts/extract_audio.py \
  --manifest manifest.csv --root ~/gcs/raw --out-dir ~/haixia-work/audio \
  --include '影片/02 針灸/' --include '影片/03 本草/' \
  --include '影片/04 黃帝內經/' --include '影片/05 傷寒論/' \
  --include '影片/06 金匱要略/' --include '影片/09 倪海厦 《八綱辨證》/' \
  --include '影片/10 倪海厦 《臨牀案例》/' \
  --include '影片/01 天紀/' --include '影片/11 倪海厦 《六壬》/'
bash scripts/gpu_job.sh status extract
bash scripts/gpu_job.sh log extract
```

`bash scripts/gpu_job.sh status extract` 顯示 `DONE exit=0` 後，上傳音訊並開始轉錄：

```bash
rclone copy ~/haixia-work/audio/ gcs:haixiani-bot-data-507014/audio/

ENGINE=whisper
EXTRA_ARGS=()
bash scripts/gpu_job.sh start transcribe -- python3 scripts/transcribe.py \
  --engine "$ENGINE" "${EXTRA_ARGS[@]}" \
  --audio-root ~/haixia-work/audio --out-dir ~/haixia-work/transcripts
bash scripts/gpu_job.sh status transcribe
bash scripts/gpu_job.sh log transcribe
```

`bash scripts/gpu_job.sh status transcribe` 顯示 `DONE exit=0` 後上傳逐字稿：

```bash
rclone copy ~/haixia-work/transcripts/ gcs:haixiani-bot-data-507014/transcripts/
```

預計約 221 小時的 16 kHz 單聲道 FLAC 音訊約 15 GB，200 GB 開機磁碟放得下。仍可按課程前綴分批處理；確認音訊與逐字稿已上傳到 GCS 後，才清理本機檔案。逐字稿的時間都相對原始媒體檔開頭，可直接引用片段出處。

VM 被收回（Spot）或因其他原因停止、重開機後，systemd 服務不會自動回來，`status` 會顯示 `RUN active=inactive`。**在 movie-nas 上**執行 `bash scripts/create_gpu_vm.sh --start --yes`，再用腳本印出的那行（帶 `--internal-ip`）重新 SSH。進入 GPU VM 後重新掛載 bucket，用同一行 `gpu_job.sh start` 重跑；已完成的輸出會跳過。把本機尚未上傳的音訊或逐字稿先用上面的 `rclone copy` 補傳。若原 zone 啟動時沒有資源，保留磁碟和結果，稍後重試；不要建立同名第二台 VM。

## 8. 轉換梁冬對話倪海廈的七個 LRC

七個 `.Lrc` 和同名 `.mp3` 都在 `文字資料/07.倪海厦国学堂/国学堂-倪海厦对话梁冬MP3(（守候诚实）淘宝店）/`。下列迴圈逐集找音訊、用 ffprobe 取得秒數，並將 `--source` 設為 `raw/` 底下 mp3 的相對路徑；缺少對應 mp3 時會警告並略過。

```bash
cd ~/HaixiaNi_bot
source ~/haixia-work/env.sh
RAW_ROOT="$HOME/gcs/raw"
LRC_DIR="$RAW_ROOT/文字資料/07.倪海厦国学堂/国学堂-倪海厦对话梁冬MP3(（守候诚实）淘宝店）"
OUT_ROOT="$HOME/haixia-work/lrc-transcripts"
shopt -s nullglob
for LRC in "$LRC_DIR"/*.Lrc; do
  MEDIA="${LRC%.Lrc}.mp3"
  if [[ ! -f "$MEDIA" ]]; then
    printf '警告：找不到對應 mp3，略過：%s\n' "$LRC" >&2
    continue
  fi
  SOURCE="${MEDIA#"$RAW_ROOT"/}"
  DURATION=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$MEDIA") || continue
  OUT="$OUT_ROOT/$SOURCE.json"
  mkdir -p "$(dirname "$OUT")"
  python3 scripts/lrc_to_transcript.py "$LRC" --source "$SOURCE" --duration "$DURATION" --out "$OUT"
done

rclone copy "$OUT_ROOT/" gcs:haixiani-bot-data-507014/transcripts/
```

## 9. 停機或刪除 VM

**回到 movie-nas** 執行。保留開機磁碟供後續續跑時停機；全數結果確認已上傳後再刪除（含開機磁碟）。

```bash
cd ~/HaixiaNi_bot
bash scripts/create_gpu_vm.sh --stop --yes
# 確認不再需要 VM 後，改用：
bash scripts/create_gpu_vm.sh --delete --yes
```

刪除 VM 後，腳本會印出刪除防火牆規則 `haixia-gpu-ssh-from-movie-nas` 的指令；確認之後不再建 GPU VM，先問使用者，再執行。

---

## 遇到問題

| 狀況 | 處理方式 |
|---|---|
| 工作跑到一半被殺掉（開機約 30 分鐘後） | DLVM 的 unattended-upgrades 會更新套件、重新載入 systemd 並重啟服務，把 tmux 裡的工作殺掉。重跑 `setup_gpu.sh` 關掉自動更新，用 `systemctl is-enabled apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service` 確認三個都是 `disabled`。長時間工作一律用 `gpu_job.sh start`，不要放在 tmux 裡。 |
| Spot 被收回 | 從 movie-nas 用 `--start --yes` 重新啟動；掛載 bucket，用同一行 `gpu_job.sh start` 重跑，已完成檔案會跳過。長時間工作改用 `PROVISIONING=STANDARD`（預設）。 |
| zone 缺貨（`ZONE_RESOURCE_POOL_EXHAUSTED`） | 建立腳本會優先改試錯誤訊息 `zonesAvailable` 列出的 zone，再試其他 zone，每個 zone 一次。全部缺貨時腳本會列出各 zone 的原因，稍後重跑。已建立 VM 啟動失敗時不要另建同名 VM。 |
| SSH 連線逾時 | 專案防火牆只允許使用者家裡的 IP。確認防火牆規則 `haixia-gpu-ssh-from-movie-nas` 存在、VM 帶 `haixia-gpu` tag，並且是從 movie-nas 用 `gcloud compute ssh haixia-gpu --zone <zone> --internal-ip` 連線。 |
| CUDA／cuDNN 錯誤 | 先跑 `nvidia-smi`；首次開機若驅動未就緒，稍後重跑 `setup_gpu.sh`。確認有先 `source ~/haixia-work/env.sh` 載入 cuBLAS/cuDNN 路徑（`gpu_job.sh` 會自動載入），再看腳本末尾煙霧測試。 |
| ModelScope 下載失敗 | 確認網路與可用磁碟空間，重跑 `setup_gpu.sh`；已下載的模型會從快取沿用。 |
