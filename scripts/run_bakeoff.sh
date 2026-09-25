#!/usr/bin/env bash
# 從 TSV 切出比較片段，逐一跑五種設定；重跑時略過既有結果。
set -uo pipefail

if [[ $# -ne 2 ]]; then
  echo "用法：$0 <raw 掛載目錄> <工作目錄>" >&2
  exit 2
fi
raw_root=$1
work_dir=$2
script_dir=$(cd "$(dirname "$0")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)
python_bin=${PYTHON_BIN:-python3}
mkdir -p "$work_dir/audio" "$work_dir/results"
exec > >(tee -a "$work_dir/bakeoff.log") 2>&1
printf '開始比較：%s\n' "$(date '+%Y-%m-%d %H:%M:%S')"

while IFS=$'\t' read -r label source start duration rest; do
  [[ $label == label || -z $label ]] && continue
  audio="$work_dir/audio/$label.flac"
  if [[ -f $audio ]]; then
    printf '音檔已存在，跳過：%s\n' "$audio"
  else
    printf '切出片段：%s（%s 秒起，長 %s 秒）\n' "$label" "$start" "$duration"
    if ! "$python_bin" "$script_dir/extract_audio.py" "$raw_root/$source" "$audio" --start "$start" --duration "$duration"; then
      printf '切音失敗，略過：%s\n' "$source"
      continue
    fi
  fi
  for setting in whisper-prompt whisper-noprompt whisper-batched sensevoice paraformer; do
    target="$work_dir/results/$setting/$label.json"
    if [[ -f $target ]]; then
      printf '結果已存在，跳過：%s\n' "$target"
      continue
    fi
    mkdir -p "$(dirname "$target")"
    printf '轉錄：%s / %s\n' "$label" "$setting"
    if ! "$python_bin" "$script_dir/transcribe.py" --engine "$setting" "$audio" \
      --source "$source" --clip-start "$start" --out "$target" \
      --prompts "$repo_root/data/course_prompts.json"; then
      printf '轉錄失敗：%s / %s\n' "$label" "$setting"
    fi
  done
done < "$repo_root/data/bakeoff_clips.tsv"
printf '比較結束：%s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
