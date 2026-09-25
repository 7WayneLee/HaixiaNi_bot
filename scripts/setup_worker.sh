#!/usr/bin/env bash
# 在 worker VM（Debian 12）上安裝需要的工具：rclone、ffmpeg、tmux、fuse3（給 rclone mount 用）
set -euo pipefail

sudo apt-get update
sudo apt-get install -y ffmpeg tmux fuse3 curl unzip python3

if ! command -v rclone >/dev/null; then
  curl -fsSL https://rclone.org/install.sh | sudo bash
fi

echo
echo "安裝完成："
rclone version | head -1
ffprobe -version | head -1
