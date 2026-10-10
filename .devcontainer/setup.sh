#!/bin/bash
# DiziVerse Pipeline - GitHub Codespaces Setup
# Run once when the Codespace is created.
set -e

echo "=== DiziVerse Codespaces Setup ==="

# 1. System packages
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg git curl > /dev/null 2>&1
echo "  ffmpeg: $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3)"

# 2. Python packages
echo "[2/5] Installing Python packages..."
pip install -q --upgrade pip
pip install -q yt-dlp faster-whisper edge-tts opencv-python-headless \
    google-api-python-client google-auth-httplib2 google-auth-oauthlib \
    requests cloudflare 2>&1 | tail -1

# 3. yt-dlp plugins (PO Token / bgutil for YouTube)
echo "[3/5] Installing yt-dlp plugins..."
mkdir -p ~/.config/yt-dlp/plugins
pip install -q bgutil-ytdlp-pot-provider 2>&1 | tail -1 || echo "  (bgutil optional, skipping)"

# 4. Copy deployment package (user uploads deployment-package/ to repo root)
echo "[4/5] Setting up pipeline files..."
if [ -d "deployment-package" ]; then
    mkdir -p ~/diziverse/{ch2,ch3,ch4}
    cp deployment-package/ch2/diziverse.py ~/diziverse/ch2/ 2>/dev/null || true
    cp deployment-package/ch2/token.json ~/diziverse/ch2/ 2>/dev/null || true
    cp deployment-package/ch3/diziverse.py ~/diziverse/ch3/ 2>/dev/null || true
    cp deployment-package/ch3/token.json ~/diziverse/ch3/ 2>/dev/null || true
    cp deployment-package/ch3/cookies.txt ~/diziverse/ch3/ 2>/dev/null || true
    cp deployment-package/ch4/diziverse.py ~/diziverse/ch4/ 2>/dev/null || true
    cp deployment-package/ch4/cookies.txt ~/diziverse/ch4/ 2>/dev/null || true
    chmod 600 ~/diziverse/ch*/token.json ~/diziverse/ch*/cookies.txt 2>/dev/null || true
    echo "  Pipeline files copied to ~/diziverse/"
else
    echo "  WARNING: deployment-package/ not found in repo root."
    echo "  Upload it, then run: bash .devcontainer/setup.sh"
fi

# 5. Verify
echo "[5/5] Verifying..."
python3 -c "import yt_dlp; print('  yt-dlp:', yt_dlp.version.__version__)"
python3 -c "import cv2; print('  opencv:', cv2.__version__)"
python3 -c "import edge_tts; print('  edge-tts: OK')"
echo ""
echo "=== Setup complete ==="
echo "Pipeline: ~/diziverse/ch2|ch3|ch4/diziverse.py"
echo "Run: cd ~/diziverse/ch2 && python3 diziverse.py 'YOUTUBE_URL'"
echo ""
echo "NOTE: Codespace stops after 30 min idle. Keep terminal active during processing."
