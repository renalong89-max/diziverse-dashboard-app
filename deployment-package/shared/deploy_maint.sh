#!/bin/bash
# =====================================================================
# DiziVerse maintenance API — ONE-PASTE deploy for Oracle Cloud Shell.
#
# The user pastes this ENTIRE block into the Cloud Shell terminal and
# presses Enter. Idempotent: safe to re-run (restarts service + tunnel).
# Emits clear OK / FAILURE lines; exits non-zero on any failure.
# =====================================================================
set -u

CH2="$HOME/diziverse-server/ch2"
PORT=8899
KEY='ALoZSGO1uwy7V7ClngS9Eb86bIq9gFFVCM8a0K1jVqA'

echo "== [1/6] ch2 directory =="
mkdir -p "$CH2" || { echo "FAILURE: cannot create $CH2"; exit 1; }
echo "OK: $CH2"

echo "== [2/6] writing maint_server.py =="
cat > "$CH2/maint_server.py" << 'PYEOF_MAINT_9X7Z'
#!/usr/bin/env python3
"""
DiziVerse maintenance API (channel 2) — narrowly scoped, key-protected.

Stdlib only. Listens on 127.0.0.1:8899; exposed via a Cloudflare quick
tunnel at deploy time.

Why a separate service: the main dashboard's server-side source is not
available in the workspace (only its Windows launcher client is), so
"extending" it would mean reimplementing the live dashboard blind — an
unacceptable risk to the user's production dashboard. This service does
ONLY the five maintenance actions below. No general shell, no arbitrary
file read/write, no secrets in responses.

Endpoints (every request requires ?key=<pre-shared-key>):
  GET  /api/maint/health          worker state, queue/failed counts,
                                  disk + memory, worker.log tail (50 lines)
  POST /api/maint/clear-failed    backup failed.txt, then empty it
  POST /api/maint/deploy-cookies  validate + install a new cookies.txt
  POST /api/maint/restart-worker  restart worker only when idle (409 if active)
  GET  /api/maint/token-check     YouTube OAuth validity via Google APIs
  POST /api/maint/deploy-pending  watcher pushes the pending-promo list
                                  (JSON {items:[{title,channel,added_utc}]});
                                  stored as pending_display.json (600)
  GET  /pending                   human page: pending promos waiting to upload

Every maintenance action is appended to ch2/maint_audit.log.
Secrets (the key, token.json, cookies content, client_secret) are NEVER
returned in responses and NEVER written to logs.
"""

import hmac
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse as urlparse
from urllib import request as urlreq

# ---------------------------------------------------------------- paths ---
CH2 = os.path.expanduser("~/diziverse-server/ch2")  # tests may override
PORT = 8899
AUDIT_NAME = "maint_audit.log"
KEY_NAME = ".maint_key"
TS_FMT = "%Y%m%d_%H%M%S"
PENDING_NAME = "pending_display.json"


def _p(name):
    """Absolute path of a fixed file inside ch2. No user-supplied paths."""
    return os.path.join(CH2, name)


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts():
    return datetime.now(timezone.utc).strftime(TS_FMT)


def _pkt(utc_s):
    """'2026-09-27T02:57:00Z' -> '27 Sep, 07:57'. Best-effort, PKT (UTC+5)."""
    try:
        s = str(utc_s or "")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(timezone(timedelta(hours=5)))
        return local.strftime("%d %b, %H:%M")
    except Exception:
        return (str(utc_s)[:16] if utc_s else "-")


# ------------------------------------------------------------------ key ---
_KEY = None


def _key():
    """Pre-shared key, read once from ch2/.maint_key (mode 600)."""
    global _KEY
    if _KEY is None:
        with open(_p(KEY_NAME), "r", encoding="utf-8") as f:
            _KEY = f.read().strip()
        if not _KEY:
            raise RuntimeError("empty maintenance key")
    return _KEY


def _audit(action, detail=""):
    """Append one audit line. Callers must never pass secret material."""
    try:
        with open(_p(AUDIT_NAME), "a", encoding="utf-8") as f:
            f.write("%s %s %s\n" % (_utcnow(), action, detail))
    except Exception:
        pass  # audit must never break the request


# ------------------------------------------------------------- helpers ---
def _nonempty_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln for ln in (l.strip() for l in f) if ln]
    except FileNotFoundError:
        return []


def _log_tail(path, n=50):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return lines[-n:]
    except FileNotFoundError:
        return []


def _unique_backup(base, ext="", sep="_"):
    """base + sep + timestamp (+ ext); add _2, _3... on collision.

    Examples: _unique_backup("failed_backup", ".txt")
              -> failed_backup_20260926_173015.txt
              _unique_backup("cookies.txt.bak", sep="-")
              -> cookies.txt.bak-20260926_173015
    """
    ts = _ts()
    cand = "%s%s%s%s" % (base, sep, ts, ext)
    i = 2
    while os.path.exists(_p(cand)):
        cand = "%s%s%s_%d%s" % (base, sep, ts, i, ext)
        i += 1
    return cand


def _worker_running():
    """True if any live process looks like the pipeline worker.

    Scans /proc directly (no external tools). The maintenance server's
    own process is excluded by name.
    """
    me = os.getpid()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == me:
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode(
                        "utf-8", "replace")
            except Exception:
                continue
            if "diziverse.py" in cmd and "maint_server.py" not in cmd:
                return True
    except Exception:
        pass
    return False


def _disk_free_pct():
    try:
        du = shutil.disk_usage(CH2)
        return round(100.0 * du.free / du.total, 1) if du.total else 0.0
    except Exception:
        return -1.0


def _mem_free_mb():
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except Exception:
        pass
    return -1


# ------------------------------------------------------- cookie checks ---
def _validate_cookies(text):
    """Netscape cookie file sanity check. Returns (ok, reason, stats)."""
    if not isinstance(text, str):
        return False, "cookies must be a string", {}
    if len(text) < 300:
        return False, "file too small (%d bytes)" % len(text), {}
    if len(text) > 2_000_000:
        return False, "file too large", {}
    lines = text.splitlines()
    head = "\n".join(lines[:5])
    if "Netscape HTTP Cookie File" not in head:
        return False, "missing Netscape cookie file header", {}
    rows = [l for l in lines
            if l and not l.startswith("#") and l.count("\t") >= 6]
    if len(rows) < 5:
        return False, "only %d cookie rows found" % len(rows), {}
    return True, "", {"lines": len(lines), "cookie_rows": len(rows)}


# ------------------------------------------------------------- tokens ---
def _token_doc():
    with open(_p("token.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _refresh_access_token(doc):
    """Exchange refresh_token for an access token (stdlib only)."""
    cid = doc.get("client_id")
    csec = doc.get("client_secret")
    if not cid or not csec:
        # fall back to a client_secrets.json next to token.json
        try:
            with open(_p("client_secrets.json"), "r", encoding="utf-8") as f:
                inst = json.load(f).get("installed", {})
            cid = cid or inst.get("client_id")
            csec = csec or inst.get("client_secret")
        except Exception:
            pass
    if not cid or not csec or not doc.get("refresh_token"):
        raise RuntimeError("token.json missing client_id/secret/refresh_token")
    data = urlparse.urlencode({
        "grant_type": "refresh_token",
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": doc["refresh_token"],
    }).encode()
    req = urlreq.Request("https://oauth2.googleapis.com/token", data=data,
                         method="POST")
    with urlreq.urlopen(req, timeout=15) as r:
        body = json.load(r)
    tok = body.get("access_token")
    if not tok:
        # Google error payloads carry a short "error" code, safe to relay.
        raise RuntimeError(str(body.get("error", "no access_token"))[:80])
    return tok


def _fetch_channel_title(access_token):
    req = urlreq.Request(
        "https://www.googleapis.com/youtube/v3/channels"
        "?mine=true&part=snippet",
        headers={"Authorization": "Bearer " + access_token})
    with urlreq.urlopen(req, timeout=15) as r:
        body = json.load(r)
    items = body.get("items", [])
    if not items:
        raise RuntimeError("no channel on this account")
    return items[0].get("snippet", {}).get("title", "?")


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "DiziVerseMaint/1.0"

    def log_message(self, fmt, *args):  # keep stdout quiet; audit log covers us
        pass

    # -- plumbing ------------------------------------------------------
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, max_bytes=2_000_000):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > max_bytes:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def _authed(self):
        try:
            qs = urlparse.parse_qs(urlparse.urlsplit(self.path).query)
            got = qs.get("key", [""])[0]
            return bool(got) and hmac.compare_digest(got, _key())
        except Exception:
            return False

    # -- dispatch ------------------------------------------------------
    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        if not self._authed():
            self._send(403, {"error": "forbidden"})
            return
        path = urlparse.urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if self.command == "GET" and path == "/api/maint/health":
                self._send(200, self._health())
            elif self.command == "POST" and path == "/api/maint/clear-failed":
                self._send(200, self._clear_failed())
            elif self.command == "POST" and path == "/api/maint/deploy-cookies":
                code, obj = self._deploy_cookies()
                self._send(code, obj)
            elif self.command == "POST" and path == "/api/maint/restart-worker":
                code, obj = self._restart_worker()
                self._send(code, obj)
            elif self.command == "GET" and path == "/api/maint/token-check":
                self._send(200, self._token_check())
            elif self.command == "POST" and path == "/api/maint/deploy-pending":
                code, obj = self._deploy_pending()
                self._send(code, obj)
            elif self.command == "GET" and path == "/pending":
                self._send_html(200, self._pending_page())
            elif path in ("/", "/api/maint"):
                self._send(200, {"service": "diziverse-maint",
                                 "endpoints": [
                                     "GET /api/maint/health",
                                     "POST /api/maint/clear-failed",
                                     "POST /api/maint/deploy-cookies",
                                     "POST /api/maint/restart-worker",
                                     "GET /api/maint/token-check",
                                     "POST /api/maint/deploy-pending",
                                     "GET /pending"]})
            else:
                self._send(404, {"error": "not found"})
        except Exception:
            self._send(500, {"error": "internal"})

    # -- endpoints -----------------------------------------------------
    def _health(self):
        return {
            "worker_running": _worker_running(),
            "queue_len": len(_nonempty_lines(_p("queue.txt"))),
            "failed_count": len(_nonempty_lines(_p("failed.txt"))),
            "done_count": len(_nonempty_lines(_p("done.txt"))),
            "disk_free_pct": _disk_free_pct(),
            "mem_free_mb": _mem_free_mb(),
            "worker_log_tail": _log_tail(_p("worker.log"), 50),
            "ts": _utcnow(),
        }

    def _clear_failed(self):
        src = _p("failed.txt")
        lines = _nonempty_lines(src)
        if not lines:
            _audit("clear-failed", "cleared=0 (already empty)")
            return {"backup": None, "cleared": 0}
        backup = _unique_backup("failed_backup", ".txt")
        shutil.copy2(src, _p(backup))
        with open(src, "w", encoding="utf-8") as f:
            f.write("")
        _audit("clear-failed", "backup=%s cleared=%d" % (backup, len(lines)))
        return {"backup": backup, "cleared": len(lines)}

    def _deploy_cookies(self):
        doc = self._read_json()
        text = doc.get("cookies") if isinstance(doc, dict) else None
        ok, reason, stats = _validate_cookies(text)
        if not ok:
            _audit("deploy-cookies", "rejected: %s" % reason)
            return 400, {"error": "invalid cookie file", "reason": reason}
        target = _p("cookies.txt")
        backup = None
        if os.path.exists(target):
            backup = _unique_backup("cookies.txt.bak", sep="-")
            shutil.copy2(target, _p(backup))
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            os.close(fd)
            raise
        os.chmod(target, 0o600)  # O_TRUNC alone does not reset an old mode
        _audit("deploy-cookies",
               "lines=%d cookie_rows=%d backup=%s"
               % (stats["lines"], stats["cookie_rows"], backup))
        return 200, {"ok": True, "lines": stats["lines"],
                     "cookie_rows": stats["cookie_rows"], "backup": backup}

    def _restart_worker(self):
        if _worker_running():
            _audit("restart-worker", "refused: worker active")
            return 409, {"error": "worker is active, refusing restart"}
        cmds = [l.strip() for l in _nonempty_lines(_p("worker_cmd.txt"))
                if not l.strip().startswith("#")]
        if not cmds:
            _audit("restart-worker", "refused: worker_cmd.txt not configured")
            return 503, {"error": "worker_cmd.txt not configured on server"}
        cmd = " && ".join(cmds)
        logf = open(_p("worker_restart.log"), "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=CH2, stdin=subprocess.DEVNULL,
                stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True)
        finally:
            logf.close()
        _audit("restart-worker", "started pid=%d" % proc.pid)
        return 200, {"ok": True, "started": True, "pid": proc.pid}

    def _token_check(self):
        try:
            doc = _token_doc()
            tok = _refresh_access_token(doc)
            title = _fetch_channel_title(tok)
            _audit("token-check", "valid=true")
            return {"valid": True, "channel": title}
        except Exception as e:
            # Never include token material: only the short error label.
            msg = str(e)[:120].replace("\n", " ")
            _audit("token-check", "valid=false")
            return {"valid": False, "error": msg}

    def _deploy_pending(self):
        """Watcher pushes the pending-promo list; stored for the /pending page.

        Body: {"items": [{"title":..., "channel":..., "added_utc":...}]}.
        Titles are the user's own pipeline data (not secrets); the audit log
        records only the count.
        """
        doc = self._read_json(max_bytes=500_000)
        items = doc.get("items") if isinstance(doc, dict) else None
        if not isinstance(items, list) or len(items) > 200:
            _audit("deploy-pending", "rejected: bad items")
            return 400, {"error": "items must be a list (max 200)"}
        clean = []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title", ""))[:200].strip()
            if not title:
                continue
            clean.append({
                "title": title,
                "channel": str(it.get("channel", ""))[:80],
                "added_utc": str(it.get("added_utc", ""))[:40],
            })
        payload = {"updated_utc": _utcnow(), "count": len(clean),
                   "items": clean}
        tmp = _p(PENDING_NAME + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _p(PENDING_NAME))
        _audit("deploy-pending", "count=%d" % len(clean))
        return 200, {"ok": True, "count": len(clean)}

    def _pending_page(self):
        """Human-readable pending list (Roman Urdu UI). Auto-refreshes."""
        import html as _html
        try:
            with open(_p(PENDING_NAME), "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            payload = {}
        items = payload.get("items") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            items = []
        upd = _pkt(payload.get("updated_utc", "")) \
            if isinstance(payload, dict) else "-"
        rows = []
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                continue
            t = _html.escape(str(it.get("title", "")))
            c = _html.escape(str(it.get("channel", "")))
            a = _pkt(str(it.get("added_utc", "")))
            rows.append(
                "<div class='item'><div class='n'>%d</div>"
                "<div class='b'><div class='t'>%s</div>"
                "<div class='m'>%s &middot; %s</div></div></div>"
                % (i, t, c, a))
        body = ("\n".join(rows) if rows else
                "<div class='empty'>Koi pending promo nahi — sab clear ✨</div>")
        return (
            "<!DOCTYPE html><html lang='ur'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta http-equiv='refresh' content='120'>"
            "<title>Pending Promos — DiziVerse</title>"
            "<style>"
            "body{font-family:system-ui,sans-serif;background:#14171c;color:#e8eaf0;"
            "margin:0;padding:16px;max-width:720px}"
            "h1{font-size:20px;margin:4px 0}"
            ".sub{color:#9aa3b2;font-size:13px;margin-bottom:14px}"
            ".item{display:flex;gap:10px;background:#1d2129;border:1px solid #2a2f3a;"
            "border-radius:10px;padding:10px 12px;margin-bottom:8px}"
            ".n{color:#f5a623;font-weight:700;min-width:22px}"
            ".t{font-size:14px;line-height:1.35}"
            ".m{color:#9aa3b2;font-size:12px;margin-top:3px}"
            ".empty{color:#9aa3b2;text-align:center;padding:40px 0}"
            "</style></head><body>"
            "<h1>⏳ Pending Promos</h1>"
            "<div class='sub'>%d waiting &middot; updated %s (PKT) &middot; "
            "auto-refresh 2 min</div>"
            "%s</body></html>" % (len(rows), _html.escape(upd), body))


def main():
    os.makedirs(CH2, exist_ok=True)
    _key()  # fail fast if the key file is missing
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print("diziverse maint api on 127.0.0.1:%d (ch2=%s)" % (PORT, CH2),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
PYEOF_MAINT_9X7Z
python3 -m py_compile "$CH2/maint_server.py" \
  && echo "OK: maint_server.py syntax OK" \
  || { echo "FAILURE: maint_server.py has syntax errors"; exit 1; }

echo "== [3/6] writing key file =="
printf '%s' "$KEY" > "$CH2/.maint_key"
chmod 600 "$CH2/.maint_key" "$CH2/maint_server.py"
echo "OK: .maint_key (600)"

if [ ! -f "$CH2/worker_cmd.txt" ]; then
  cat > "$CH2/worker_cmd.txt" << 'CMDEOF'
# DiziVerse worker launch command, used by the maintenance API's
# POST /api/maint/restart-worker. One shell command per line
# (lines starting with # are ignored).
# The agent will fill this in after one inspection of how the
# pipeline worker is normally started on this server.
# Example:
# cd ~/diziverse-server/ch2 && nohup python3 worker_loop.sh >> worker.log 2>&1 &
CMDEOF
  echo "OK: worker_cmd.txt placeholder created"
  echo "     (restart-worker reports 'not configured' until a real"
  echo "      command is placed here)"
fi

echo "== [4/6] (re)starting maintenance service =="
pkill -f "[m]aint_server.py" 2>/dev/null || true
sleep 1
cd "$CH2"
nohup python3 maint_server.py > "$CH2/maint_server.log" 2>&1 &
SRVPID=$!
sleep 2
if ! kill -0 "$SRVPID" 2>/dev/null; then
  echo "FAILURE: service died on start. Last log lines:"
  tail -20 "$CH2/maint_server.log" 2>/dev/null || true
  exit 1
fi
echo "OK: service running (pid $SRVPID), log: $CH2/maint_server.log"

echo "== [5/6] local health check =="
if python3 - "$KEY" << 'HEALTHEOF'
import json, sys, urllib.request
key = sys.argv[1]
with urllib.request.urlopen(
        "http://127.0.0.1:8899/api/maint/health?key=" + key,
        timeout=15) as r:
    h = json.load(r)
print("health: worker_running=%s queue=%d failed=%d disk_free=%s%% mem_free=%sMB"
      % (h["worker_running"], h["queue_len"], h["failed_count"],
         h["disk_free_pct"], h["mem_free_mb"]))
HEALTHEOF
then
  echo "OK: health check passed"
else
  echo "FAILURE: health check failed"
  exit 1
fi

echo "== [6/6] cloudflare quick tunnel =="
export PATH="$HOME/bin:$PATH"
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not found -> downloading (free, official build)"
  mkdir -p "$HOME/bin"
  ARCH="$(uname -m)"
  case "$ARCH" in
    aarch64|arm64) CFARCH="arm64" ;;
    x86_64|amd64)  CFARCH="amd64" ;;
    *) echo "FAILURE: unsupported arch $ARCH"; exit 1 ;;
  esac
  curl -fsSL -o "$HOME/bin/cloudflared" \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CFARCH" \
    || { echo "FAILURE: cloudflared download failed"; exit 1; }
  chmod +x "$HOME/bin/cloudflared"
  echo "OK: cloudflared installed to $HOME/bin"
fi
pkill -f "[c]loudflared.*$PORT" 2>/dev/null || true
sleep 1
nohup cloudflared tunnel --url "http://localhost:$PORT" \
  > /tmp/maint_tunnel.log 2>&1 &
echo "waiting for tunnel URL (up to 60s)..."
TURL=""
for i in $(seq 1 60); do
  TURL="$(grep -o 'https://[A-Za-z0-9.-]*\.trycloudflare\.com' \
          /tmp/maint_tunnel.log 2>/dev/null | head -1)"
  [ -n "$TURL" ] && break
  sleep 1
done
if [ -z "$TURL" ]; then
  echo "FAILURE: tunnel URL did not appear. Log tail:"
  tail -20 /tmp/maint_tunnel.log 2>/dev/null || true
  exit 1
fi
printf '%s' "$TURL/?key=$KEY" > "$CH2/maint_url.txt"
chmod 600 "$CH2/maint_url.txt"
echo "OK: tunnel up"

echo ""
echo "==================== SUCCESS ===================="
echo "Maintenance API is live."
echo "URL (save this — it changes on every server restart):"
echo "  $TURL/?key=$KEY"
echo "Also saved on server: $CH2/maint_url.txt"
echo "================================================="
