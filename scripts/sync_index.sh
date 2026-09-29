#!/usr/bin/env bash
# 透過 movie-nas 同步索引產物；只用 copy 與 check，不刪除任何東西。
# 傳送前一定先列出檔案與總大小，確認後才傳（--yes 可略過確認）。
set -euo pipefail

usage() {
  cat <<'HELP'
用法：
  scripts/sync_index.sh push <本機目錄> [--yes]
      把 chunks.jsonl、build_report.json、index.sqlite 傳到 movie-nas，
      再複製到 gs://<bucket>/index/，最後用 rclone check --one-way 核對。
  scripts/sync_index.sh push-embed [--yes]
      在 movie-nas 上把 embeddings.f16.npy、embeddings.meta.json 複製到 gs://<bucket>/index/ 並核對。
  scripts/sync_index.sh pull-embed <本機目錄> [--yes]
      把 movie-nas 上的 embeddings.f16.npy、embeddings.meta.json 拉回本機。
  scripts/sync_index.sh pull-ocr <本機目錄> [--yes]
      把 movie-nas 上的文字辨識結果（ocr/*.pages.json）拉回本機的 <本機目錄>/ocr/。

環境變數：HAIXIA_BUCKET、HAIXIA_SSH_HOST、HAIXIA_GCS_REMOTE、HAIXIA_REMOTE_DIR（movie-nas 家目錄底下，預設 haixia-index-build）。
HELP
}

if [[ ${1:-} == --help || ${1:-} == -h || $# -lt 1 ]]; then usage; exit 0; fi
mode=$1
shift
local_dir=''
if [[ $mode != push-embed ]]; then
  if (( $# < 1 )); then usage >&2; exit 2; fi
  local_dir=$1
  shift
fi
yes=0
if (( $# )); then
  if [[ $# -ne 1 || $1 != --yes ]]; then usage >&2; exit 2; fi
  yes=1
fi
bucket=${HAIXIA_BUCKET:-haixiani-bot-data-507014}
host=${HAIXIA_SSH_HOST:-movie-nas}
remote=${HAIXIA_GCS_REMOTE:-gcs}
remote_dir=${HAIXIA_REMOTE_DIR:-haixia-index-build}
ssh_opts=(-o ClearAllForwardings=yes -o ConnectTimeout=20)
build_files=(chunks.jsonl build_report.json index.sqlite)
embed_files=(embeddings.f16.npy embeddings.meta.json)

confirm() {
  if (( yes )); then return 0; fi
  read -r -p "確定要傳送嗎？[y/N] " answer
  [[ $answer == y || $answer == Y ]] || { echo "已取消"; exit 1; }
}

# 在遠端列出檔案大小與總和；參數是遠端目錄與檔名（相對於該目錄）。
remote_list() {
  ssh "${ssh_opts[@]}" "$host" bash -s -- "$@" <<'REMOTE'
set -euo pipefail
dir=$HOME/$1; shift
total=0
for name in "$@"; do
  for file in "$dir"/$name; do
    if [[ -f $file ]]; then
      size=$(wc -c < "$file" | tr -d " ")
      total=$((total + size))
      printf '  %12d  %s\n' "$size" "${file#$dir/}"
    fi
  done
done
awk -v t="$total" 'BEGIN { printf "  合計 %d bytes（%.1f MiB）\n", t, t / 1048576 }'
REMOTE
}

case "$mode" in
  push)
    if [[ ! -d $local_dir ]]; then echo "找不到本機目錄：$local_dir" >&2; exit 2; fi
    files=()
    total=0
    echo "要傳送的檔案（$local_dir → $host:~/$remote_dir → gs://$bucket/index/）："
    for name in "${build_files[@]}"; do
      if [[ -f $local_dir/$name ]]; then
        size=$(stat -f %z "$local_dir/$name")
        total=$((total + size))
        files+=("$name")
        printf '  %12d  %s\n' "$size" "$name"
      fi
    done
    if (( ${#files[@]} == 0 )); then echo "沒有可以傳的檔案" >&2; exit 2; fi
    awk -v t="$total" 'BEGIN { printf "  合計 %d bytes（%.1f MiB）\n", t, t / 1048576 }'
    confirm
    ssh "${ssh_opts[@]}" "$host" "mkdir -p \"\$HOME/$remote_dir\""
    COPYFILE_DISABLE=1 tar -cf - -C "$local_dir" "${files[@]}" | ssh "${ssh_opts[@]}" "$host" "tar -xf - -C \"\$HOME/$remote_dir\""
    ssh "${ssh_opts[@]}" "$host" bash -s -- "$remote" "$bucket" "$remote_dir" "${files[@]}" <<'REMOTE'
set -euo pipefail
remote=$1; bucket=$2; dir=$HOME/$3; shift 3
list=$(mktemp)
printf '%s\n' "$@" > "$list"
rclone copy --files-from "$list" "$dir/" "${remote}:${bucket}/index/"
rclone check --one-way --files-from "$list" "$dir/" "${remote}:${bucket}/index/"
rm -f "$list"
REMOTE
    ;;
  push-embed)
    echo "要從 $host:~/$remote_dir 複製到 gs://$bucket/index/ 的檔案："
    remote_list "$remote_dir" "${embed_files[@]}"
    confirm
    ssh "${ssh_opts[@]}" "$host" bash -s -- "$remote" "$bucket" "$remote_dir" "${embed_files[@]}" <<'REMOTE'
set -euo pipefail
remote=$1; bucket=$2; dir=$HOME/$3; shift 3
list=$(mktemp)
for name in "$@"; do [[ -f $dir/$name ]] && printf '%s\n' "$name"; done > "$list"
if [[ ! -s $list ]]; then echo "找不到向量檔" >&2; exit 2; fi
rclone copy --files-from "$list" "$dir/" "${remote}:${bucket}/index/"
rclone check --one-way --files-from "$list" "$dir/" "${remote}:${bucket}/index/"
rm -f "$list"
REMOTE
    ;;
  pull-embed|pull-ocr)
    if [[ $mode == pull-embed ]]; then names=("${embed_files[@]}"); target=$local_dir; else names=('ocr/*.pages.json'); target=$local_dir; fi
    echo "要從 $host:~/$remote_dir 拉回 $target 的檔案："
    remote_list "$remote_dir" "${names[@]}"
    confirm
    mkdir -p "$target"
    ssh "${ssh_opts[@]}" "$host" bash -s -- "$remote_dir" "${names[@]}" <<'REMOTE' | tar -xf - -C "$target"
set -euo pipefail
cd "$HOME/$1"; shift
files=()
for name in "$@"; do for file in $name; do [[ -f $file ]] && files+=("$file"); done; done
if (( ${#files[@]} == 0 )); then echo "遠端沒有檔案" >&2; exit 2; fi
tar -cf - "${files[@]}"
REMOTE
    # 核對：遠端與本機的 SHA-256 必須相同。
    remote_sums=$(ssh "${ssh_opts[@]}" "$host" bash -s -- "$remote_dir" "${names[@]}" <<'REMOTE'
set -euo pipefail
cd "$HOME/$1"; shift
for name in "$@"; do for file in $name; do [[ -f $file ]] && sha256sum "$file"; done; done
REMOTE
)
    local_sums=$(cd "$target" && while read -r _sum file; do shasum -a 256 "$file"; done <<< "$remote_sums")
    if [[ $(awk '{print $1, $2}' <<< "$remote_sums") != $(awk '{print $1, $2}' <<< "$local_sums") ]]; then
      echo "核對失敗：本機與遠端的 SHA-256 不同" >&2
      exit 1
    fi
    echo "已拉回並核對 SHA-256：$target"
    ;;
  *) usage >&2; exit 2 ;;
esac
