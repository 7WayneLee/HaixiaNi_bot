#!/usr/bin/env bash
# 透過 movie-nas 複製逐字稿；只用 copy 與 check，不刪除 GCS 物件。
set -euo pipefail

usage() {
  cat <<'HELP'
用法：
  scripts/sync_transcripts.sh pull <本機目錄> [--include <來源路徑前綴>]
  scripts/sync_transcripts.sh push <本機目錄>

環境變數：HAIXIA_BUCKET、HAIXIA_SSH_HOST、HAIXIA_GCS_REMOTE。
pull 會先從 GCS 複製 ASR 至中轉機，再以 tar 傳回本機。
push 會以 tar 傳到中轉機，複製至 GCS，最後用 rclone check --one-way 核對。
HELP
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then usage; exit 0; fi
if (( $# < 2 )); then usage >&2; exit 2; fi
mode=$1
local_dir=$2
shift 2
bucket=${HAIXIA_BUCKET:-haixiani-bot-data-507014}
host=${HAIXIA_SSH_HOST:-movie-nas}
remote=${HAIXIA_GCS_REMOTE:-gcs}
ssh_opts=(-o ClearAllForwardings=yes -o ConnectTimeout=20)

case "$mode" in
  pull)
    prefix=''
    if (( $# )); then
      if [[ $# -ne 2 || $1 != --include ]]; then usage >&2; exit 2; fi
      prefix=$2
    fi
    mkdir -p "$local_dir"
    # 遠端參數以 base64 編碼傳送，避免中文或空格被 SSH 命令列再切詞。
    prefix64=$(printf '%s' "x$prefix" | base64 | tr -d '\n')
    ssh "${ssh_opts[@]}" "$host" bash -s -- "$remote" "$bucket" <<'REMOTE'
set -euo pipefail
remote=$1; bucket=$2
mkdir -p "$HOME/haixia-sync/asr"
rclone copy "${remote}:${bucket}/transcripts/asr/" "$HOME/haixia-sync/asr/"
REMOTE
    ssh "${ssh_opts[@]}" "$host" bash -s -- "$prefix64" <<'REMOTE' | tar -xf - -C "$local_dir"
set -euo pipefail
prefix=$(printf '%s' "$1" | base64 -d)
prefix=${prefix#x}
cd "$HOME/haixia-sync/asr"
if [[ -z $prefix ]]; then
  tar -cf - .
else
  while IFS= read -r -d '' file; do
    if [[ ${file#./} == "$prefix"* ]]; then printf '%s\0' "$file"; fi
  done < <(find . -type f -print0) | tar --null -T - -cf -
fi
REMOTE
    ;;
  push)
    if (( $# )); then usage >&2; exit 2; fi
    if [[ ! -d $local_dir ]]; then echo "找不到本機目錄：$local_dir" >&2; exit 2; fi
    ssh "${ssh_opts[@]}" "$host" 'mkdir -p "$HOME/haixia-sync/corrected"'
    COPYFILE_DISABLE=1 tar -cf - -C "$local_dir" . | ssh "${ssh_opts[@]}" "$host" 'tar -xf - -C "$HOME/haixia-sync/corrected"'
    ssh "${ssh_opts[@]}" "$host" bash -s -- "$remote" "$bucket" <<'REMOTE'
set -euo pipefail
remote=$1; bucket=$2
rclone copy "$HOME/haixia-sync/corrected/" "${remote}:${bucket}/transcripts/corrected/"
rclone check --one-way "$HOME/haixia-sync/corrected/" "${remote}:${bucket}/transcripts/corrected/"
REMOTE
    ;;
  *) usage >&2; exit 2 ;;
esac
