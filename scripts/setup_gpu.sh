#!/usr/bin/env bash
# 在 GPU VM clone 專案後執行；重跑會沿用虛擬環境與模型快取。
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
venv_dir="$HOME/venv"

if ! nvidia-smi; then
    printf 'NVIDIA 驅動尚未就緒。DLVM 第一次開機會自動安裝驅動，請稍後重跑。\n' >&2
    exit 1
fi

sudo apt-get update
sudo apt-get install -y ffmpeg fuse3 tmux git curl python3-venv

if ! command -v rclone >/dev/null 2>&1; then
    installer=$(mktemp)
    trap 'rm -f "$installer"' EXIT
    curl -fsSL https://rclone.org/install.sh -o "$installer"
    sudo bash "$installer"
    rm -f "$installer"
    trap - EXIT
fi

if ! command -v python3.12 >/dev/null 2>&1; then
    printf '找不到 Python 3.12；請確認 DLVM 映像檔。\n' >&2
    exit 1
fi
python3.12 -m venv "$venv_dir"
python="$venv_dir/bin/python"
"$python" -m pip install --upgrade pip

# CUDA 版 PyTorch 必須先裝；從 requirements-gpu.txt 的安裝註解取得 wheel 索引。
torch_index_url=${TORCH_INDEX_URL:-$(grep -Eo 'https://download.pytorch.org/whl/cu[0-9]+' "$repo_dir/requirements-gpu.txt" | head -n 1 || true)}
if [[ -z "$torch_index_url" ]]; then
    printf 'requirements-gpu.txt 註解未提供 CUDA PyTorch wheel 索引。\n' >&2
    exit 1
fi
"$python" -m pip install torch torchaudio --index-url "$torch_index_url"
"$python" -m pip install -r "$repo_dir/requirements-gpu.txt"
"$python" -m pip install nvidia-cublas-cu12 'nvidia-cudnn-cu12==9.*'

# 用目前虛擬環境的 site-packages 找出套件附帶的共享函式庫。
lib_paths=$("$python" - <<'PY'
from pathlib import Path
import sysconfig

paths = []
base = Path(sysconfig.get_paths()["purelib"])
for package in ("cublas", "cudnn"):
    directory = base / "nvidia" / package / "lib"
    if directory.is_dir():
        paths.append(str(directory))
if len(paths) != 2:
    raise SystemExit("找不到 cuBLAS/cuDNN 共享函式庫")
print(":".join(paths))
PY
)
export LD_LIBRARY_PATH="$lib_paths:${LD_LIBRARY_PATH:-}"
bashrc_line="export LD_LIBRARY_PATH=\"$lib_paths:\${LD_LIBRARY_PATH:-}\""
touch "$HOME/.bashrc"
if ! grep -Fqx "$bashrc_line" "$HOME/.bashrc"; then
    printf '\n# HaixiaNi GPU 虛擬環境的 cuBLAS/cuDNN\n%s\n' "$bashrc_line" >> "$HOME/.bashrc"
fi

if ! rclone listremotes | grep -Fxq 'gcs:'; then
    rclone config create gcs "google cloud storage" env_auth=true bucket_policy_only=true
fi

printf '預先下載 Whisper 與 FunASR 模型；已快取的檔案會沿用。\n'
"$python" - <<'PY'
from huggingface_hub import snapshot_download as hf_download
from modelscope.hub.snapshot_download import snapshot_download as ms_download

hf_download("Systran/faster-whisper-large-v3")
for model in (
    "iic/SenseVoiceSmall",
    "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
):
    print(f"下載／確認模型：{model}", flush=True)
    ms_download(model)
PY

"$python" - <<'PY'
import torch
import ctranslate2

print(f"torch.cuda.is_available(): {torch.cuda.is_available()}")
print(f"ctranslate2.get_cuda_device_count(): {ctranslate2.get_cuda_device_count()}")
PY
printf '設定完成。新 shell 可執行 source ~/venv/bin/activate。\n'
