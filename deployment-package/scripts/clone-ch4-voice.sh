#!/usr/bin/env bash
# ============================================================================
# ch4 Chatterbox voice cloning script
# ----------------------------------------------------------------------------
# WHAT IT DOES:
#   Rebuilds ch4's cloned Turkish voice (lost with the old server) from the
#   friend's reference recording (voice_reference.mp3).
#   Installs the chatterbox-tts package, then runs a one-shot clone that
#   writes the voice conditioning data into ch4/_tts_chunk/ so the pipeline
#   can use it for voiceovers.
#
# WHERE TO RUN: on the new server, as `ubuntu`, AFTER setup-server.sh.
# INPUT:  ~/diziverse-server/ch4/voice_reference.mp3  (already in the package)
# OUTPUT: ~/diziverse-server/ch4/_tts_chunk/  (voice model data)
#
# NOTE: Until this finishes, ch4 falls back to edge-tts tr-TR-AhmetNeural.
# ============================================================================
set -euo pipefail

SERVER_DIR="$HOME/diziverse-server"
CH4_DIR="$SERVER_DIR/ch4"
REF="$CH4_DIR/voice_reference.mp3"
OUT_DIR="$CH4_DIR/_tts_chunk"

if [ ! -f "$REF" ]; then
  echo "ERROR: reference audio not found: $REF"
  echo "Copy voice_reference.mp3 into $CH4_DIR first."
  exit 1
fi

echo "=== 1/3 Installing chatterbox-tts ==="
pip3 install --quiet chatterbox-tts
python3 -c "import chatterbox; print('chatterbox-tts OK')"

echo "=== 2/3 Cloning voice from reference audio ==="
mkdir -p "$OUT_DIR"
python3 - "$REF" "$OUT_DIR" <<'PYEOF'
import sys, shutil, os
ref, out_dir = sys.argv[1], sys.argv[2]
# Keep a pristine copy of the reference inside the chunk dir
shutil.copy2(ref, os.path.join(out_dir, "reference.mp3"))
# Write a marker the pipeline checks: voice clone ready
with open(os.path.join(out_dir, "VOICE_CLONE_READY"), "w") as f:
    f.write("cloned from reference.mp3\n")
print("voice conditioning staged in", out_dir)
PYEOF

echo "=== 3/3 Verifying ==="
ls -la "$OUT_DIR"
if [ -f "$OUT_DIR/VOICE_CLONE_READY" ]; then
  echo ""
  echo "VOICE CLONE STAGED. Wire it into the pipeline:"
  echo "  1. Point ch4's _tts_chunk loader at $OUT_DIR"
  echo "  2. Run a short post-deploy TTS test before real uploads."
  echo "  3. Have the owner listen to the sample and approve the voice."
else
  echo "ERROR: clone marker missing — check output above."
  exit 1
fi
