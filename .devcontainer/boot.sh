#!/bin/bash
# DiziVerse boot hook — .devcontainer/devcontainer.json me postStartCommand se
# har codespace start par chalta hai (idle-stop ke baad API-restart par bhi).
# 1) tunnel-keeper zinda na ho to launch karta hai (dashboard :80 + dashboard
#    tunnel + maint :8899 ka self-heal keeper khud karta hai).
# 2) maint tunnel (cloudflared -> :8899) keeper kabhi nahi chhuta — is liye yahan
#    pgrep guard ke sath start hota hai.
# 3) Dono fresh tunnel URLs repo ki `codespace-tunnel-urls` branch me
#    `codespace-tunnel-urls/live-urls.json` par push hoti hain taake VM-side
#    watchdog bina login ke resync kar sake.
# Idempotent: pehle se chal raha ho to kuch nahi — double-start safe.
S="$HOME/diziverse-server"
LOG="$S/keeper.log"
REPO_OWNER="renalong89-max"
REPO_NAME="diziverse-dashboard-app"
RENDEZ_BRANCH="codespace-tunnel-urls"
RENDEZ_PATH="codespace-tunnel-urls/live-urls.json"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] boot: $*" >> "$LOG"; }

[ -d "$S" ] || { log "server dir nahi mila: $S — kuch nahi kiya"; exit 0; }

# --- 1. tunnel-keeper ---
if pgrep -f "tunnel-keeper.sh" >/dev/null 2>&1; then
  log "keeper pehle se chal raha hai — skip"
else
  if [ -x "$S/tunnel-keeper.sh" ]; then
    log "keeper start kar raha hoon"
    ( cd "$S" && nohup bash "$S/tunnel-keeper.sh" >> "$LOG" 2>&1 < /dev/null & )
  else
    log "WARN: tunnel-keeper.sh nahi mila: $S"
  fi
fi

# --- 2. maint tunnel (cloudflared -> 127.0.0.1:8899) ---
MAINT_PATTERN="cloudflared tunnel --url http://localhost:8899"
MAINT_TUNLOG="$S/tunnel-maint.log"
if pgrep -f "$MAINT_PATTERN" >/dev/null 2>&1; then
  log "maint tunnel pehle se chal raha hai — skip"
else
  CLOUDFLARED="$(command -v cloudflared 2>/dev/null || echo cloudflared)"
  log "maint tunnel start kar raha hoon (-> :8899)"
  : > "$MAINT_TUNLOG"
  ( nohup "$CLOUDFLARED" tunnel --url http://localhost:8899 --no-autoupdate \
      >> "$MAINT_TUNLOG" 2>&1 < /dev/null & )
fi

# --- 3. fresh URLs ka intezar + rendezvous push ---
tunnel_url() { grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$1" 2>/dev/null | head -1; }
DASH_URL=""; MAINT_URL=""
for i in $(seq 1 45); do
  [ -z "$DASH_URL" ] && DASH_URL="$(tunnel_url "$S/tunnel-dashboard.log")"
  [ -z "$DASH_URL" ] && [ -f "$S/dashboard_tunnel_url.txt" ] && DASH_URL="$(head -1 "$S/dashboard_tunnel_url.txt" 2>/dev/null | tr -d '[:space:]')"
  [ -z "$MAINT_URL" ] && MAINT_URL="$(tunnel_url "$MAINT_TUNLOG")"
  [ -n "$DASH_URL" ] && [ -n "$MAINT_URL" ] && break
  sleep 2
done
log "dashboard tunnel: ${DASH_URL:-nahi mila} | maint tunnel: ${MAINT_URL:-nahi mila}"

if [ -z "$DASH_URL" ] && [ -z "$MAINT_URL" ]; then
  log "dono URLs nahi mileen — rendezvous push skip"
  exit 0
fi

JSON="$(python3 -c "
import json
print(json.dumps({'ts': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
 'codespace': 'psychic-robot-p7vv5q7674j37xw6',
 'dashboard_url': '$DASH_URL', 'maint_url': '$MAINT_URL',
 'complete': bool('$DASH_URL' and '$MAINT_URL')}))")"
B64="$(printf '%s' "$JSON" | base64 -w0 2>/dev/null || printf '%s' "$JSON" | base64)"

gh_push() {
  # $1 = b64 content, $2 = sha (empty for create)
  local data
  if [ -n "$2" ]; then
    data="$(python3 -c "import json;print(json.dumps({'message':'codespace boot: fresh tunnel URLs','content':'$1','branch':'$RENDEZ_BRANCH','sha':'$2'}))")"
  else
    data="$(python3 -c "import json;print(json.dumps({'message':'codespace boot: fresh tunnel URLs','content':'$1','branch':'$RENDEZ_BRANCH'}))")"
  fi
  if command -v gh >/dev/null 2>&1; then
    printf '%s' "$data" | gh api "repos/$REPO_OWNER/$REPO_NAME/contents/$RENDEZ_PATH" --method PUT --input - 2>&1 | head -c 300
  elif [ -n "${GITHUB_TOKEN:-}" ]; then
    curl -sS -m 30 -X PUT "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/contents/$RENDEZ_PATH" \
      -H "Authorization: Bearer $GITHUB_TOKEN" -H "Content-Type: application/json" \
      -d "$data" 2>&1 | head -c 300
  else
    echo "NO_AUTH"
  fi
}

SHA=""
if command -v gh >/dev/null 2>&1; then
  SHA="$(gh api "repos/$REPO_OWNER/$REPO_NAME/contents/$RENDEZ_PATH?ref=$RENDEZ_BRANCH" --jq .sha 2>/dev/null || true)"
elif [ -n "${GITHUB_TOKEN:-}" ]; then
  SHA="$(curl -sS -m 30 "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/contents/$RENDEZ_PATH?ref=$RENDEZ_BRANCH" \
    -H "Authorization: Bearer $GITHUB_TOKEN" 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin).get('sha',''))" 2>/dev/null || true)"
fi
OUT="$(gh_push "$B64" "$SHA")"
log "rendezvous push: $(printf '%s' "$OUT" | head -c 200)"
