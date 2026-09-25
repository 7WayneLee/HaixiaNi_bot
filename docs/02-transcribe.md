# 第二步：抽音訊、比較轉錄引擎，再全量轉錄

目標：先用四段各 10 分鐘的影音比較五種轉錄設定，校對其中 15 分鐘作為參考答案；選定設定後處理已確認的倪師課程。原始資料保留在 `raw/`，音訊與逐字稿分別存入 bucket 的 `audio/`、`transcripts/`。

```
GCS raw/ ──rclone 唯讀掛載──► GPU VM ──extract_audio.py──► audio/
                                    │                       │
                                    └─ transcribe.py ◄──────┘ ──► transcripts/
```

## 1. 費用與守則

GPU VM 使用 us-central1 的 L4 Spot。Spot 可能在工作中被收回；執行時 GPU、CPU 和磁碟計費，停機後開機磁碟仍計費。建立、啟動、停止或刪除雲端資源前，先依 `CLAUDE.md` 的工作守則取得使用者同意。用完當天就停機或刪除，不要讓 GPU 閒置。

## 2. 建立 GPU VM

**在 movie-nas 上**執行；這台 VM 已有 gcloud，Mac 不用安裝。先預覽，再明確建立：

```bash
ssh movie-nas
cd ~/HaixiaNi_bot
bash scripts/create_gpu_vm.sh
bash scripts/create_gpu_vm.sh --yes
```

腳本會查詢 us-central1 提供 nvidia-l4 的 zone。某個 zone 沒有 Spot 資源時會試下一個；其他錯誤會停止。成功後會印出 `gcloud compute ssh haixia-gpu --zone <zone>`，在 movie-nas 執行該行進入 GPU VM。預設機型為 `g2-standard-4`、200GB 平衡型開機磁碟，映像檔為 CUDA 12.9 / Ubuntu 24.04 的 NVIDIA DLVM。

## 3. 設定 GPU VM

**在 GPU VM 上**執行：

```bash
git clone https://github.com/7WayneLee/HaixiaNi_bot.git
cd ~/HaixiaNi_bot
bash scripts/setup_gpu.sh
source ~/venv/bin/activate
```

已 clone 過就進入 `~/HaixiaNi_bot`，更新專案後重跑 `setup_gpu.sh`。腳本會安裝 ffmpeg、rclone、Python 套件、CUDA 版 PyTorch，預先下載 Whisper、SenseVoice、Paraformer、VAD 和標點模型，最後印出 PyTorch 與 CTranslate2 看得到的 CUDA 裝置數。第一次開機的 NVIDIA 驅動可能還在自動安裝；若 `nvidia-smi` 失敗，稍後再執行同一行。

## 4. 掛載 bucket

在 GPU VM 上把 bucket 唯讀掛載，原始檔不會被修改：

```bash
mkdir -p ~/gcs
rclone mount gcs:haixiani-bot-data-507014 ~/gcs --read-only --daemon
rclone copy gcs:haixiani-bot-data-507014/meta/manifest.csv ~/HaixiaNi_bot/
```

重新開機後要重跑 `rclone mount`。掛載目錄下的 `~/gcs/raw` 是以下指令的原始資料根目錄。

## 5. 在 tmux 跑小規模比較

```bash
tmux new -s bakeoff
cd ~/HaixiaNi_bot
source ~/venv/bin/activate
mkdir -p ~/haixia-work
bash scripts/run_bakeoff.sh ~/gcs/raw ~/haixia-work
rclone copy ~/haixia-work/results/ gcs:haixiani-bot-data-507014/bakeoff/results/
rclone copy ~/haixia-work/bakeoff.log gcs:haixiani-bot-data-507014/bakeoff/
```

按 `Ctrl+b`、放開後按 `d` 可離開 tmux，工作繼續跑。之後用 `tmux attach -t bakeoff` 查看。小規模比較的五組結果位於 `~/haixia-work/results/<設定>/<label>.json`。把四個評分區間各自校對成 UTF-8 純文字，存到 `~/haixia-work/references/<label>.txt`：`shanghan`、`zhenjiu`、`bencao` 各 240 秒，`bagang` 180 秒；`speaker` 只確認講者，不評分。每行可用 `[mm:ss]` 或 `[hh:mm:ss]` 起頭，`#` 開頭的行會略過。

## 6. 評分並選擇設定

```bash
cd ~/HaixiaNi_bot
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

報告列出每段的字錯率（CER）、中醫詞召回率、速度（RTF）和低信心段落比例。CER 越低、召回率越高越好；RTF 是處理秒數除以音訊秒數。結果檔缺少時標為「（缺）」，參考答案缺少時標為「（缺參考答案）」；補齊後再決定全量設定。

## 7. 全量抽音訊與轉錄

只處理已確認的倪師課程：人紀影片、八綱辨證、臨床案例、天紀、六壬。`MP3 人紀全` 與影片重複，其他國學堂講者不納入；梁冬對話倪海廈的七集走下一節的 LRC。下列指令可在 tmux 裡分批執行；`--include` 可重複指定來源路徑前綴。小規模比較的「設定」和 `transcribe.py` 的引擎參數對照如下：

| 比較設定 | `ENGINE` | 額外參數 |
|---|---|---|
| `whisper-prompt` | `whisper` | 無，預設使用課程提示詞 |
| `whisper-noprompt` | `whisper` | `--no-prompt` |
| `whisper-batched` | `whisper-batched` | 無 |
| `sensevoice` | `sensevoice` | 無 |
| `paraformer` | `paraformer` | 無 |

把下方的 `ENGINE` 設為選定引擎；若選 `whisper-noprompt`，把 `EXTRA_ARGS=()` 改成 `EXTRA_ARGS=(--no-prompt)`。

```bash
tmux new -s transcribe
cd ~/HaixiaNi_bot
source ~/venv/bin/activate
python3 scripts/extract_audio.py --manifest manifest.csv --root ~/gcs/raw \
  --out-dir ~/haixia-work/audio \
  --include '影片/02 針灸/' --include '影片/03 本草/' \
  --include '影片/04 黃帝內經/' --include '影片/05 傷寒論/' \
  --include '影片/06 金匱要略/' --include '影片/09 倪海厦 《八綱辨證》/' \
  --include '影片/10 倪海厦 《臨牀案例》/' \
  --include '影片/01 天紀/' --include '影片/11 倪海厦 《六壬》/' \
  2>&1 | tee ~/haixia-work/extract.log
rclone copy ~/haixia-work/audio/ gcs:haixiani-bot-data-507014/audio/

ENGINE=whisper
EXTRA_ARGS=()
python3 scripts/transcribe.py --engine "$ENGINE" "${EXTRA_ARGS[@]}" --audio-root ~/haixia-work/audio \
  --out-dir ~/haixia-work/transcripts 2>&1 | tee ~/haixia-work/transcribe.log
rclone copy ~/haixia-work/transcripts/ gcs:haixiani-bot-data-507014/transcripts/
```

預計約 221 小時的 16 kHz 單聲道 FLAC 音訊約 15 GB，200 GB 開機磁碟放得下。仍可按課程前綴分批處理；確認音訊與逐字稿已上傳到 GCS 後，才清理本機檔案。逐字稿的時間都相對原始媒體檔開頭，可直接引用片段出處。

Spot 被收回後 VM 會停止。**在 movie-nas 上**執行 `bash scripts/create_gpu_vm.sh --start --yes`，再用腳本印出的 zone 重新 SSH。進入 GPU VM 後重新掛載 bucket、開 tmux，重跑同一批指令；已完成的輸出會跳過。把本機尚未上傳的音訊或逐字稿先用上面的 `rclone copy` 補傳。若原 zone 啟動時沒有 Spot 資源，保留磁碟和結果，稍後重試；不要建立同名第二台 VM。

## 8. 轉換梁冬對話倪海廈的七個 LRC

七個 `.Lrc` 和同名 `.mp3` 都在 `文字資料/07.倪海厦国学堂/国学堂-倪海厦对话梁冬MP3(（守候诚实）淘宝店）/`。下列迴圈逐集找音訊、用 ffprobe 取得秒數，並將 `--source` 設為 `raw/` 底下 mp3 的相對路徑；缺少對應 mp3 時會警告並略過。

```bash
cd ~/HaixiaNi_bot
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

---

## 遇到問題

| 狀況 | 處理方式 |
|---|---|
| Spot 被收回 | 從 movie-nas 用 `--start --yes` 重新啟動；掛載 bucket，重開 tmux 並重跑原批次，已完成檔案會跳過。 |
| zone 沒有 L4 Spot 資源 | 建立腳本會自動試下一個 zone；若全部不足，稍後重跑。已建立 VM 啟動失敗時不要另建同名 VM。 |
| CUDA／cuDNN 錯誤 | 先跑 `nvidia-smi`；首次開機若驅動未就緒，稍後重跑 `setup_gpu.sh`。新 shell 用 `source ~/.bashrc` 載入 cuBLAS/cuDNN 路徑，再看腳本末尾煙霧測試。 |
| ModelScope 下載失敗 | 確認網路與可用磁碟空間，重跑 `setup_gpu.sh`；已下載的模型會從快取沿用。 |
