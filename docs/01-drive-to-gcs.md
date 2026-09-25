# 第一步：把 Google Drive 的資料搬到 GCS，並盤點

目標：

1. 把 Drive 上的倪師資料（約 100GB）複製到 GCS bucket。整個過程都在雲端完成，不經過你的 Mac。
2. 算出影片、音訊的總時數和檔案類型，用來估算第二步（轉錄）的時間和費用。

```
Google Drive ──rclone──► haixia-worker（小 VM）──► GCS bucket（raw/）
                                  │
                                  └─ inventory.py 盤點 ──► manifest.csv
```

費用：這台 VM（e2-small）每小時約 0.02 美元；GCS 存 100GB 每月約 2 美元；資料傳進 GCP 不收費。

---

## 已經有 VM、rclone 也登入過 Drive 的情況

可以跳過第 3 步（建 VM）和第 5 步（rclone 登入 Drive）。在現有的 VM 上先確認下面三件事，再從第 6 步繼續：

**(a) rclone remote 的名稱**

```bash
rclone listremotes
```

腳本預設 Drive 的 remote 叫 `gdrive`、GCS 的叫 `gcs`。名稱不同時，執行腳本前加上 `DRIVE_REMOTE=` / `GCS_REMOTE=` 即可，
例如：`DRIVE_REMOTE=mydrive bash scripts/transfer.sh "中醫" $BUCKET`。
如果清單裡還沒有 GCS 的 remote，照第 6 步建立即可。

**(b) VM 有沒有 GCS 的寫入權限**

```bash
curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/scopes
```

輸出裡要有 `devstorage.read_write`、`devstorage.full_control` 或 `cloud-platform` 其中之一。
在 console 用預設值建立的 VM，通常只有 `devstorage.read_only`，這樣寫入 GCS 會失敗。修正方法：
console → Compute Engine → VM 執行個體 → **停止** VM → 編輯 →「存取權範圍」選「為每個 API 設定存取權」→
**Storage 改成「讀取/寫入」** → 儲存 → 重新啟動 VM。

**(c) VM 在哪一區**

```bash
curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone
```

bucket 請維持在 `us-central1`，因為第二步的 GPU VM 也會開在那裡。VM 在其他區也可以搬，只是跨區傳 100GB 會多幾美元的流量費。

另外還要執行一次 `bash scripts/setup_worker.sh`，補裝 ffmpeg、tmux、fuse3。已經裝過的 rclone 會自動略過。

> `BUCKET` 也可以是「現有 bucket 底下的資料夾」，例如 `export BUCKET=my-bucket/haixiani-bot-data`。
> 這樣寫法的話，第 2 步建 bucket 和最後的 403 處理指令要用 bucket 本身的名稱（`my-bucket`）；其他步驟照常。

---

## 1. 開 Cloud Shell，設定專案

在 GCP console 右上角點 `>_` 圖示打開 Cloud Shell，貼上：

```bash
gcloud config set project vmdemo1-507014
gcloud services enable compute.googleapis.com storage.googleapis.com

export BUCKET=haixiani-bot-data-507014
export ZONE=us-central1-a
```

> bucket 名稱全世界不能重複。下一步如果顯示名稱已被使用，就把 `BUCKET` 換成別的名稱再試一次。

## 2. 建立 bucket

```bash
gcloud storage buckets create gs://$BUCKET \
  --location=us-central1 \
  --uniform-bucket-level-access
```

## 3. 建立 worker VM

```bash
gcloud compute instances create haixia-worker \
  --zone=$ZONE \
  --machine-type=e2-small \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=20GB \
  --scopes=storage-rw
```

`--scopes=storage-rw` 讓這台 VM 可以直接讀寫 GCS，不用另外處理金鑰。
rclone 搬資料時是邊下載邊上傳，不會存在 VM 硬碟上，所以 20GB 的硬碟就夠用。

## 4. 連進 VM，安裝工具

用 SSH 連進 VM。在 console 的「Compute Engine → VM 執行個體」點 haixia-worker 旁的 **SSH** 按鈕即可。
也可以在 Cloud Shell 執行：

```bash
gcloud compute ssh haixia-worker --zone=us-central1-a
```

（第一次連線會問要不要建立 SSH 金鑰，一路按 Enter 就好。）

進入 VM 後執行：

```bash
echo 'export BUCKET=haixiani-bot-data-507014' >> ~/.bashrc   # 如果第 1 步換過名稱，這裡也要改
source ~/.bashrc

sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/7WayneLee/HaixiaNi_bot.git
cd HaixiaNi_bot
bash scripts/setup_worker.sh
```

## 5. 讓 rclone 連上你的 Google Drive

VM 沒有瀏覽器，所以登入要借用你的 Mac 完成，只需要做一次。

**在 VM 上**執行 `rclone config`，依照下表回答：

| rclone 問 | 你回答 |
|---|---|
| `n) New remote` … | `n` |
| `name>` | `gdrive` |
| `Storage>` | `drive` |
| `client_id>` | 直接按 Enter |
| `client_secret>` | 直接按 Enter |
| `scope>` | `drive.readonly`（唯讀，rclone 不會改動你 Drive 裡的任何檔案） |
| `service_account_file>` | 直接按 Enter |
| `Edit advanced config?` | `n` |
| `Use web browser to automatically authenticate…?` | **`n`** |

這時 rclone 會顯示一行指令，長得像 `rclone authorize "drive" "eyJ..."`，先整行複製起來。

**在 Mac 的「終端機」**執行：

```bash
brew install rclone
# 沒有 Homebrew 的話改用：sudo -v ; curl https://rclone.org/install.sh | sudo bash

rclone authorize "drive" "eyJ..."    # 貼上剛剛複製的那一整行
```

瀏覽器會自動打開。登入放資料的那個 Google 帳號並按「允許」後，Mac 終端機會印出一大段 token。
把它**整段**複製，貼回 VM 上的 `config_token>`。接著：

| rclone 問 | 你回答 |
|---|---|
| `Configure this as a Shared Drive (Team Drive)?` | `n` |
| `Keep this "gdrive" remote?` | `y` |
| 回到主選單 | `q` 離開 |

確認連上了：

```bash
rclone lsd gdrive:                            # 列出 Drive 最上層的資料夾
rclone lsd gdrive: --drive-shared-with-me     # 列出「與我共用」裡的資料夾
```

應該會看到倪師資料所在的 `中醫` 資料夾。

## 6. 讓 rclone 連上 GCS

```bash
rclone config create gcs "google cloud storage" env_auth=true bucket_policy_only=true

# 寫入測試：顯示 ok 就代表權限沒問題
echo ok | rclone rcat gcs:$BUCKET/_test.txt && rclone cat gcs:$BUCKET/_test.txt && rclone deletefile gcs:$BUCKET/_test.txt
```

## 7. 開始搬資料

用 tmux 執行。這樣就算關掉 SSH 視窗或斷線，傳輸也會繼續進行。

```bash
tmux new -s transfer
cd ~/HaixiaNi_bot
bash scripts/transfer.sh "中醫" $BUCKET      # 倪師資料在 Drive 的「中醫」資料夾
```

- 資料如果在「與我共用」裡，改用：`SHARED_WITH_ME=1 bash scripts/transfer.sh "資料夾名稱" $BUCKET`
- 讓它在背景繼續跑：按 `Ctrl+b`，放開後再按 `d`
- 之後回來看進度：`tmux attach -t transfer`
- 100GB 大約需要 1 到 3 小時，看 Drive 的下載速度
- 中斷了就直接重跑同一行指令，已經搬完的檔案會自動跳過

## 8. 盤點資料

搬完之後，把 bucket 掛載成 VM 上的一個資料夾，再執行盤點：

```bash
mkdir -p ~/gcs
rclone mount gcs:$BUCKET ~/gcs --read-only --daemon

cd ~/HaixiaNi_bot
python3 scripts/inventory.py ~/gcs/raw --out ~/manifest.csv
rclone copy ~/manifest.csv gcs:$BUCKET/meta/     # 明細也存一份到 bucket
```

盤點會讀取每個影音檔的開頭來算時長，檔案多的話要跑一段時間。
**跑完後把終端機印出的摘要整段貼給我**，我會依此規劃第二步（轉錄），包括：

- 需要轉錄的時數，以及 GPU 的時間和費用
- 有字幕檔或內含字幕的影片，可以直接用字幕、省下轉錄
- 重複的檔案，不用轉兩次
- PDF、文件、壓縮檔的處理方式

## 9. 做完先把 VM 停機

```bash
sudo poweroff
```

之後 Telegram bot 可能會跑在這台 VM 上，所以先停機、不要刪掉。停機期間只收硬碟費用，每月不到 1 美元。

---

## 遇到問題

| 狀況 | 處理方式 |
|---|---|
| 寫入 GCS 出現 `403` / `does not have storage.objects.create access` | 在 Cloud Shell 執行下方指令，給 VM 寫入權限 |
| 出現 `downloadQuotaExceeded` | 別人分享的熱門檔案被下載太多次。等 24 小時後重跑，或在 Drive 上對該檔案「建立副本」到自己的雲端硬碟 |
| 出現很多 `rateLimitExceeded`、速度很慢 | rclone 會自動重試，可以不用管。若一直很慢，可以[建立自己的 client ID](https://rclone.org/drive/#making-your-own-client-id) |
| `rclone lsd gdrive:` 看不到資料夾 | 可能在「與我共用」，加上 `--drive-shared-with-me` 再試 |
| 盤點時 `rclone mount` 失敗 | 確認第 4 步的 `setup_worker.sh` 有跑完（需要 fuse3） |

403 的處理指令：

```bash
PROJECT_NUMBER=$(gcloud projects describe vmdemo1-507014 --format='value(projectNumber)')
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role=roles/storage.objectAdmin
```
