#!/usr/bin/env bash
# ============================================================================
# DiziVerse Ampere A1 server setup script
# Run ONCE on the fresh Ubuntu ARM server as the `ubuntu` user.
# Installs: Python3, ffmpeg, yt-dlp + plugins, Python deps, Cloudflare tunnel.
# Creates: ~/diziverse-server/{ch2,ch3,ch4} directory structure.
# ============================================================================
set -euo pipefail

echo "=== 1/6 Updating apt ==="
sudo apt-get update -qq

echo "=== 2/6 Installing system packages (python3, pip, ffmpeg, curl) ==="
sudo apt-get install -y -qq python3 python3-pip python3-venv ffmpeg curl unzip > /dev/null
python3 --version
ffmpeg -version | head -1

echo "=== 3/6 Installing yt-dlp + plugins ==="
sudo curl -L -s https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
yt-dlp --version
# PO-token / player plugins live in ~/.config/yt-dlp/plugins on first pipeline run.

echo "=== 4/6 Installing Python packages ==="
pip3 install --quiet --upgrade pip
pip3 install --quiet \
  faster-whisper \
  edge-tts \
  opencv-python-headless \
  numpy \
  Pillow \
  requests \
  google-api-python-client \
  google-auth-oauthlib \
  google-auth-httplib2
echo "Python deps installed."

echo "=== 5/6 Installing Cloudflare tunnel binary (cloudflared) ==="
ARCH="$(uname -m)"
if [ "$ARCH" = "aarch64" ]; then
  CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
else
  CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
fi
sudo curl -L -s "$CF_URL" -o /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared
cloudflared --version

echo "=== 6/6 Creating directory structure ==="
mkdir -p ~/diziverse-server/ch2 ~/diziverse-server/ch3 ~/diziverse-server/ch4
mkdir -p ~/diziverse-server/shared ~/diziverse-server/downloads
echo "created: $(ls ~/diziverse-server)"

echo ""
echo "SETUP COMPLETE. Next: copy the deployment package files into ~/diziverse-server/."
echo "  ch2/diziverse.py + ch2/token.json          -> ~/diziverse-server/ch2/"
echo "  ch3/diziverse.py + ch3/token.json + cookies -> ~/diziverse-server/ch3/"
echo "  ch4/diziverse.py + ch4/cookies.txt          -> ~/diziverse-server/ch4/"
echo "  shared/*                                    -> ~/diziverse-server/shared/"
