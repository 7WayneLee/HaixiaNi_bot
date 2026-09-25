#!/usr/bin/env bash
# 把 Google Drive 的資料夾複製到 GCS bucket 的 raw/ 底下。
# 可以重複執行：已經複製過、沒有變動的檔案會自動跳過，中斷後直接重跑即可。
#
# 用法：bash scripts/transfer.sh <Drive 資料夾> <bucket 名稱>
# 資料在「與我共用」時：SHARED_WITH_ME=1 bash scripts/transfer.sh <Drive 資料夾> <bucket 名稱>
# rclone remote 不叫 gdrive / gcs 時：DRIVE_REMOTE=名稱 GCS_REMOTE=名稱 bash scripts/transfer.sh ...
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "用法：bash scripts/transfer.sh <Drive 資料夾> <bucket 名稱>" >&2
  exit 1
fi
src="${DRIVE_REMOTE:-gdrive}:$1"
dest="${GCS_REMOTE:-gcs}:$2/raw"

drive_flags=()
if [ "${SHARED_WITH_ME:-0}" = 1 ]; then
  drive_flags+=(--drive-shared-with-me)
fi

mkdir -p "$HOME/logs"
log="$HOME/logs/transfer-$(date +%Y%m%d-%H%M%S).log"

echo "來源：$src"
echo "目的：$dest"
echo "紀錄檔：$log"
echo

rclone copy "$src" "$dest" \
  "${drive_flags[@]}" \
  --transfers 8 \
  --checkers 16 \
  --drive-acknowledge-abuse \
  --retries 5 \
  --low-level-retries 20 \
  --log-file "$log" \
  --log-level INFO \
  --progress

echo
echo "== 大小核對（Google 文件、試算表會被轉成 docx/xlsx，大小可能略有差異）=="
echo "Drive："
rclone size "$src" "${drive_flags[@]}"
echo "GCS："
rclone size "$dest"
