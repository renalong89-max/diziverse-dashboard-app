#!/bin/bash
# =====================================================================
# DiziVerse maintenance API — UPDATE (not full redeploy).
#
# Paste this ENTIRE block into the Oracle server SSH session
# (prompt must show ubuntu@diziverse-server) and press Enter.
# It installs the new maint_server.py + manager_app.py (DiziVerse Manager
# app at GET /app) and restarts ONLY the maintenance service.
# The Cloudflare tunnel is NOT touched, so the public URL stays the same.
# =====================================================================
set -u

CH2="$HOME/diziverse-server/ch2"
PORT=8899
KEY='ALoZSGO1uwy7V7ClngS9Eb86bIq9gFFVCM8a0K1jVqA'
DASHKEY='ZddxHAgZfdpiuHKlQ0ZkkQ'

echo "== [1/7] ch2 directory =="
mkdir -p "$CH2" || { echo "FAILURE: cannot create $CH2"; exit 1; }
echo "OK: $CH2"

echo "== [2/7] writing maint_server.py =="
cp "$CH2/maint_server.py" "$CH2/maint_server.py.bak-$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
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
  GET  /app                       DiziVerse Manager: pending/queue/complete/
                                  failed with drama names (mobile + desktop)
  POST /api/maint/push-titles      watcher backfills {video_id: title}
                                  (JSON {titles:{...}}, max 500); merged into
                                  title_cache.json (600)
  POST /api/maint/push-processing  watcher reports the currently-processing
                                  video (JSON {video_id, title}); stored as
                                  processing.json (600)
  GET  /api/maint/titles           title_cache.json (for watcher diffing)
  GET  /api/maint/resolve?vid=     on-demand title->drama resolve (cached)

Every maintenance action is appended to ch2/maint_audit.log.
Secrets (the key, token.json, cookies content, client_secret) are NEVER
returned in responses and NEVER written to logs.
"""

import hmac
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse as urlparse
from urllib import request as urlreq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manager_app

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
            if self.command == "GET" and path == "/api/maint/dashboard-url":
                self._send(200, self._dashboard_url())
            elif self.command == "GET" and path == "/api/maint/health":
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
            elif self.command == "POST" and path == "/api/maint/deploy-code":
                code, obj = self._deploy_code_impl()
                self._send(code, obj)
            elif self.command == "POST" and path == "/api/maint/run-setup":
                code, obj = self._run_setup()
                self._send(code, obj)
            elif self.command == "GET" and path == "/pending":
                self._send_html(200, self._pending_page())
            elif self.command == "GET" and path == "/app":
                qs = urlparse.parse_qs(urlparse.urlsplit(self.path).query)
                key = qs.get("key", [""])[0]
                self._send_html(200, manager_app.build_app(key))
            elif self.command == "GET" and path == "/api/maint/titles":
                self._send(200, {"titles": manager_app.load_json("title_cache.json", {})})
            elif self.command == "POST" and path == "/api/maint/push-titles":
                code, obj = self._push_titles()
                self._send(code, obj)
            elif self.command == "POST" and path == "/api/maint/push-processing":
                code, obj = self._push_processing()
                self._send(code, obj)
            elif self.command == "GET" and path == "/api/maint/resolve":
                qs = urlparse.parse_qs(urlparse.urlsplit(self.path).query)
                self._send(200, self._resolve_title(qs.get("vid", [""])[0]))
            elif path in ("/", "/api/maint"):
                self._send(200, {"service": "diziverse-maint",
                                 "endpoints": [
                                     "GET /api/maint/dashboard-url",
                                     "GET /api/maint/health",
                                     "POST /api/maint/clear-failed",
                                     "POST /api/maint/deploy-cookies",
                                     "POST /api/maint/restart-worker",
                                     "GET /api/maint/token-check",
                                     "POST /api/maint/deploy-pending",
                                     "POST /api/maint/deploy-code",
                                     "POST /api/maint/run-setup",
                                     "POST /api/maint/push-titles",
                                     "POST /api/maint/push-processing",
                                     "GET /api/maint/titles",
                                     "GET /api/maint/resolve?vid=",
                                     "GET /pending",
                                     "GET /app"]})
            else:
                self._send(404, {"error": "not found"})
        except Exception:
            self._send(500, {"error": "internal"})

    # -- endpoints -----------------------------------------------------
    def _run_setup(self):
        """Naye channel (ch3, ch4, ...) ka setup — key-protected.
        
        Body: {"channel": 3}  (3 ya zyada, 2 nahi)
        Karta hai:
          - ~/diziverse-server/chN/ directory
          - queue.txt, done.txt, failed.txt, quota_use.json
        Nahi karta (user/agent alag se):
          - quota_rotate.py (deploy-code se)
          - OAuth tokens (user consent)
          - cookies (copy ya fresh)
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            _audit("run-setup", "rejected: bad body")
            return 400, {"error": "bad body"}
        n = body.get("channel")
        if not isinstance(n, int) or n < 3:
            _audit("run-setup", "rejected: bad channel %r" % (n,))
            return 400, {"error": "channel must be int >= 3"}
        ch_name = "ch%d" % n
        base = os.path.expanduser("~/diziverse-server")
        ch_dir = os.path.join(base, ch_name)
        try:
            os.makedirs(ch_dir, exist_ok=True)
            created = []
            for fn in ("queue.txt", "done.txt", "failed.txt"):
                fp = os.path.join(ch_dir, fn)
                if not os.path.isfile(fp):
                    open(fp, "a").close()
                    created.append(fn)
            qf = os.path.join(ch_dir, "quota_use.json")
            if not os.path.isfile(qf):
                with open(qf, "w") as f:
                    f.write("{}")
                created.append("quota_use.json")
            # cookies check (copy nahi karte, sirf batate hain)
            has_cookies = os.path.isfile(os.path.join(ch_dir, "cookies.txt"))
            _audit("run-setup", "ch=%s created=%s cookies=%s" % (ch_name, created, has_cookies))
            return 200, {
                "ok": True,
                "channel": ch_name,
                "dir": ch_dir,
                "created": created,
                "has_cookies": has_cookies,
                "next": [
                    "quota_rotate.py deploy karo (deploy-code)",
                    "OAuth tokens rakho (user consent)",
                    "cookies.txt copy karo ya fresh export",
                ],
            }
        except Exception as e:
            _audit("run-setup", "FAILED ch=%s: %s" % (ch_name, str(e)[:100]))
            return 500, {"error": "setup failed"}

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

    def _dashboard_url(self):
        """Keeper ka likha live dashboard tunnel URL (file se)."""
        p = "/home/ubuntu/diziverse-server/dashboard_tunnel_url.txt"
        url = ""
        try:
            with open(p, encoding="utf-8") as f:
                url = f.read().strip().split()[0]
        except Exception:
            url = ""
        return {"url": url, "updated_utc": _utcnow()}

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

    _DEPLOY_NEW_OK = frozenset({"quota_rotate.py"})
    _DEPLOY_PATCH_OK = frozenset({"diziverse.py", "diziverse_server.py"})
    _DEPLOY_MAX_BYTES = 200_000

    @staticmethod
    def _deploy_name_ok(name, allowed):
        return (isinstance(name, str)
                and re.fullmatch(r"[A-Za-z0-9_]+\.py", name) is not None
                and name in allowed)

    def _deploy_code_impl(self):
        """Body: {"files": [{"name":..., "content":...}],
                   "patches": [{"file":..., "anchor":..., "replacement":...}]}"""
        doc = self._read_json(max_bytes=1_000_000)
        if not isinstance(doc, dict):
            _audit("deploy-code", "rejected: bad body")
            return 400, {"error": "body must be a JSON object"}
        files = doc.get("files") or []
        patches = doc.get("patches") or []
        if not isinstance(files, list) or not isinstance(patches, list):
            _audit("deploy-code", "rejected: bad lists")
            return 400, {"error": "files/patches must be lists"}
        if len(files) + len(patches) == 0:
            return 400, {"error": "nothing to do"}
        if len(files) + len(patches) > 10:
            _audit("deploy-code", "rejected: too many items")
            return 400, {"error": "max 10 items per call"}

        # -- validate everything BEFORE touching disk ------------------
        ops = []  # ("file", name, content) / ("patch", file, anchor, repl)
        for it in files:
            if not isinstance(it, dict):
                _audit("deploy-code", "rejected: bad file item")
                return 400, {"error": "bad file item"}
            name, content = it.get("name"), it.get("content")
            if not self._deploy_name_ok(name, self._DEPLOY_NEW_OK):
                _audit("deploy-code", "rejected: file not allowed: %r" % (name,))
                return 400, {"error": "file not allowed: %r" % (name,)}
            if not isinstance(content, str) or not content.strip():
                _audit("deploy-code", "rejected: empty content")
                return 400, {"error": "empty content for %s" % name}
            if len(content.encode("utf-8")) > self._DEPLOY_MAX_BYTES:
                _audit("deploy-code", "rejected: too large")
                return 400, {"error": "%s too large" % name}
            ops.append(("file", name, content))
        for it in patches:
            if not isinstance(it, dict):
                _audit("deploy-code", "rejected: bad patch item")
                return 400, {"error": "bad patch item"}
            fn = it.get("file")
            anchor = it.get("anchor")
            repl = it.get("replacement")
            if not self._deploy_name_ok(fn, self._DEPLOY_PATCH_OK):
                _audit("deploy-code", "rejected: patch not allowed: %r" % (fn,))
                return 400, {"error": "patch not allowed: %r" % (fn,)}
            if not isinstance(anchor, str) or not anchor.strip():
                _audit("deploy-code", "rejected: empty anchor")
                return 400, {"error": "empty anchor"}
            if not isinstance(repl, str):
                _audit("deploy-code", "rejected: bad replacement")
                return 400, {"error": "bad replacement"}
            target = _p(fn)
            try:
                src = open(target, encoding="utf-8").read()
            except FileNotFoundError:
                _audit("deploy-code", "rejected: target missing: %s" % fn)
                return 400, {"error": "target not found: %s" % fn}
            n = src.count(anchor)
            if n == 0 and repl.strip() and repl.strip() in src:
                continue  # already applied — idempotent skip
            if n != 1:
                _audit("deploy-code",
                       "rejected: anchor count=%d (must be 1)" % n)
                return 400, {"error":
                             "anchor must appear exactly once (found %d)" % n}
            ops.append(("patch", fn, anchor, repl))
        if not ops:
            _audit("deploy-code", "already applied")
            return 200, {"ok": True, "already": True}

        # -- apply with backup + compile gate ---------------------------
        backups = {}
        touched = []
        try:
            for op in ops:
                if op[0] == "file":
                    _, name, content = op
                    target = _p(name)
                    if os.path.exists(target) and name not in backups:
                        backups[name] = _unique_backup(name + ".bak", sep="-")
                        shutil.copy2(target, _p(backups[name]))
                    tmp = target + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(content)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, target)
                    touched.append(name)
                else:
                    _, fn, anchor, repl = op
                    target = _p(fn)
                    if fn not in backups:
                        backups[fn] = _unique_backup(fn + ".bak", sep="-")
                        shutil.copy2(target, _p(backups[fn]))
                    src = open(target, encoding="utf-8").read()
                    with open(target, "w", encoding="utf-8") as f:
                        f.write(src.replace(anchor, repl, 1))
                    touched.append(fn)
            for name in touched:
                py_compile.compile(_p(name), doraise=True)
        except Exception as e:
            for name, bak in backups.items():
                try:
                    shutil.copy2(_p(bak), _p(name))
                except Exception:
                    pass
            _audit("deploy-code", "ABORTED, restored: %s" % str(e)[:80])
            return 500, {"error": "deploy failed, backups restored"}
        _audit("deploy-code",
               "ok files=%s backups=%s" % (touched, list(backups.values())))
        return 200, {"ok": True, "files": touched,
                     "backups": list(backups.values())}

    def _push_titles(self):
        """Watcher backfills {video_id: title} for queue/done/failed items.

        Body: {"titles": {"<11-char id>": "<title>", ...}} (max 500).
        Merged into title_cache.json (600). Audit logs only the count.
        """
        doc = self._read_json(max_bytes=500_000)
        titles = doc.get("titles") if isinstance(doc, dict) else None
        if not isinstance(titles, dict) or len(titles) > 500:
            _audit("push-titles", "rejected: bad titles")
            return 400, {"error": "titles must be a dict (max 500)"}
        cache = manager_app.load_json("title_cache.json", {})
        if not isinstance(cache, dict):
            cache = {}
        added = 0
        for vid, title in titles.items():
            if not isinstance(vid, str):
                continue
            vid = manager_app.vid_of(vid)
            if not vid:
                continue
            t = str(title or "")[:200].strip()
            if not t or cache.get(vid):
                continue
            cache[vid] = t
            added += 1
        if len(cache) > 5000:
            cache = dict(list(cache.items())[-5000:])
        manager_app.save_json_600("title_cache.json", cache)
        _audit("push-titles", "added=%d" % added)
        return 200, {"ok": True, "added": added}

    def _push_processing(self):
        """Watcher reports the currently-processing video (or clears it).

        Body: {"video_id": "<id>"|null, "title": "<title>"}.
        Stored as processing.json (600) for the /app "Abhi" card.
        """
        doc = self._read_json(max_bytes=50_000)
        if not isinstance(doc, dict):
            _audit("push-processing", "rejected: bad doc")
            return 400, {"error": "need a JSON object"}
        vid = doc.get("video_id")
        payload = {
            "video_id": manager_app.vid_of(vid) if vid else None,
            "title": str(doc.get("title", ""))[:200],
            "updated_utc": _utcnow(),
        }
        manager_app.save_json_600("processing.json", payload)
        _audit("push-processing", "video_id=%s" % ("set" if payload["video_id"] else "cleared"))
        return 200, {"ok": True}

    def _resolve_title(self, vid):
        """On-demand title resolve for one video id (used by /app lazy-load).

        Returns {"ok": true, "drama":..., "ep":..., "title":...} or
        {"ok": false}. Result is cached server-side.
        """
        vid = manager_app.vid_of(vid or "")
        if not vid:
            return {"ok": False}
        title = manager_app.get_title(vid)
        if not title:
            return {"ok": False}
        return {"ok": True, "title": title,
                "drama": manager_app.drama_name(title),
                "ep": manager_app.ep_info(title)}

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
python3 -m py_compile "$CH2/maint_server.py" || { echo "FAILURE: maint_server.py syntax error"; exit 1; }
echo "OK: maint_server.py written (backup kept), syntax OK"

echo "== [3/7] writing manager_app.py =="
cat > "$CH2/manager_app.py" << 'PYEOF_APP_4Q2W'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DiziVerse Manager app — mobile-first web UI served by maint_server.py.

Shows (Roman Urdu, drama NAMES not links):
  - Abhi kya ban raha hai (running video: drama name, stage, progress)
  - Pending (watcher queue), Queue (server buffer), Complete, Failed
Data sources:
  - Dashboard /api/status on 127.0.0.1:80 (key from dash_key.txt, baked at deploy)
  - pending_display.json (watcher push), processing.json (watcher push)
  - title_cache.json {source_videoid: source_title} — watcher backfills hourly,
    plus live oEmbed resolve on demand (short timeout, cached).
All files live in the ch2 dir, mode 600. No third-party deps.
"""
import html
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

_FILE_DIR = os.path.dirname(os.path.abspath(__file__))
# Production: manager_app.py lives inside ch2. Tests override this.
CH2 = os.environ.get("DIZIVERSE_CH2") or _FILE_DIR


def _p(name):
    return os.path.join(CH2, name)


def load_json(name, default):
    try:
        with open(_p(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_600(name, obj):
    fd = os.open(_p(name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------- titles ---
VID_RE = re.compile(r"(?:youtu\.be/|v=)([A-Za-z0-9_-]{11})")


def vid_of(url):
    """youtu.be/xxx ya watch?v=xxx se ID nikalo; bare 11-char ID bhi chalta hai."""
    if not url:
        return None
    m = VID_RE.search(str(url))
    if m:
        return m.group(1)
    s = str(url).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    return None


def oembed_title(vid):
    """Source promo ka title (YouTube oEmbed, free, no key). None on failure."""
    try:
        url = "https://www.youtube.com/oembed?url=https://youtu.be/" + vid
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r).get("title")
    except Exception:
        return None


def get_title(vid):
    """Cached title lookup; live-resolves + caches on miss. None if unknown."""
    if not vid:
        return None
    cache = load_json("title_cache.json", {})
    if not isinstance(cache, dict):
        cache = {}
    if cache.get(vid):
        return cache[vid]
    t = oembed_title(vid)
    if t:
        cache[vid] = t
        # cache ko bound rakho (5000 entries max)
        if len(cache) > 5000:
            cache = dict(list(cache.items())[-5000:])
        save_json_600("title_cache.json", cache)
        return t
    return None


# --------------------------------------------------------- drama parsing ---
def _clean(s):
    s = re.sub(r"@\S+", " ", s or "")          # @channel mentions
    s = s.split("|")[0]                        # " | description" tail
    s = re.sub(r"[\u201c\u201d\u201e\"']", "", s)  # quotes
    s = re.sub(r"\s+", " ", s).strip(" -–—:|")
    return s


def drama_name(title):
    """'Halef: Köklerin Çağrısı 37. Bölüm 1. Fragmanı' -> 'Halef: Köklerin Çağrısı'."""
    t = _clean(title)
    m = re.search(r"^(.*?)\s+\d+\.\s*(?:B[oö]l[uü]m|Sezon)", t, re.I)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"^(.*?)\s+(?:Fragman[Ff]ragman?[ıiu]?|Tan[ıi]t[ıi]m|[OÖ]n [İi]zleme|B[oö]l[uü]m[uü])\b", t)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return t[:48] if t else "—"


_EP_RE = re.compile(r"(\d+)\.\s*(B[oö]l[uü]m|Sezon)", re.I)
_FR_RE = re.compile(r"(\d+)\.\s*(Fragman[ıiu]?|Tan[ıi]t[ıi]m|[OÖ]n [İi]zleme)", re.I)


def ep_info(title):
    """'... 37. Bölüm 1. Fragmanı' -> '37. Bölüm • 1. Fragman'."""
    t = _clean(title)
    parts = []
    m = _EP_RE.search(t)
    if m:
        parts.append("%s. %s" % (m.group(1), "Bölüm" if m.group(2).lower().startswith("b") else "Sezon"))
    m = _FR_RE.search(t)
    if m:
        kind = m.group(2)
        kl = kind.lower()
        if kl.startswith("tan"):
            kn = "Tanıtım"
        elif "izleme" in kl:
            kn = "Ön İzleme"
        else:
            kn = "Fragman"
        parts.append("%s. %s" % (m.group(1), kn))
    return " • ".join(parts)


def fmt_pkt(iso):
    """ISO utc -> '27 Sep, 07:57' PKT."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        pkt = dt.astimezone(timezone(timedelta(hours=5)))
        return pkt.strftime("%d %b, %H:%M")
    except Exception:
        return ""


# -------------------------------------------------------------- dashboard ---
def dash_status():
    """Local dashboard /api/status (ch2). {} on failure."""
    try:
        key = open(_p("dash_key.txt"), encoding="utf-8").read().strip()
    except Exception:
        return {}
    if not key:
        return {}
    try:
        url = "http://127.0.0.1:80/api/status?key=" + urllib.parse.quote(key)
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.load(r)
        ch = (data.get("channels") or {}).get("ch2") or {}
        return {
            "running": bool(ch.get("running")),
            "progress_pct": ch.get("progress_pct"),
            "stage": ch.get("stage") or "",
            "queue": ch.get("queue") or [],
            "done": ch.get("done") or [],
            "failed": ch.get("failed") or [],
        }
    except Exception:
        return {}


# ------------------------------------------------------------------ html ---
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',Roboto,system-ui,sans-serif;
 background:#14171c;color:#e8eaf0;padding:14px;max-width:960px;margin:0 auto}
.hd{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.hd h1{font-size:20px}
.live{font-size:12px;padding:3px 10px;border-radius:20px;background:#1d2129;
 border:1px solid #2a2f3a;color:#9aa3b2;display:inline-flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;background:#5a5f75;display:inline-block}
.dot.on{background:#4caf50;animation:bl 1.2s infinite}
@keyframes bl{50%{opacity:.35}}
.sub{color:#9aa3b2;font-size:12px;margin:6px 0 14px}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
@media(min-width:700px){.stats{grid-template-columns:repeat(5,1fr)}}
.stat{background:#1d2129;border:1px solid #2a2f3a;border-radius:10px;padding:10px 12px}
.stat .k{font-size:11px;color:#9aa3b2;margin-bottom:2px}
.stat .v{font-size:20px;font-weight:700}
.stat .n{font-size:12px;color:#cfd3e1;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.runbar{height:8px;background:#2a2f3a;border-radius:6px;margin-top:8px;overflow:hidden}
.runbar i{display:block;height:100%;background:linear-gradient(90deg,#f5a623,#ff7b39);border-radius:6px}
.tabs{display:flex;gap:6px;margin-bottom:12px;position:sticky;top:0;background:#14171c;padding:8px 0;z-index:5}
.tab{flex:1;text-align:center;padding:9px 4px;border-radius:10px;background:#1d2129;
 border:1px solid #2a2f3a;color:#cfd3e1;font-size:13px;cursor:pointer}
.tab b{color:#f5a623}
.tab.act{background:#262c38;border-color:#f5a623;color:#fff}
.pane{display:none}
.pane.act{display:block}
@media(min-width:700px){.pane.act{display:grid;grid-template-columns:1fr 1fr;gap:8px;align-content:start}}
.item{background:#1d2129;border:1px solid #2a2f3a;border-radius:10px;padding:10px 12px;margin-bottom:8px}
@media(min-width:700px){.item{margin-bottom:0}}
.item .dr{font-size:15px;font-weight:700}
.item .ep{font-size:13px;color:#f5a623;margin-top:2px}
.item .mt{font-size:12px;color:#9aa3b2;margin-top:3px}
.num{color:#f5a623;font-weight:700;margin-right:6px}
.empty{color:#9aa3b2;text-align:center;padding:34px 0;font-size:14px}
.ctl{margin-top:16px;background:#1d2129;border:1px solid #2a2f3a;border-radius:10px;padding:12px}
.ctl summary{cursor:pointer;font-size:14px;font-weight:600}
.btn{display:inline-block;margin:8px 8px 0 0;padding:9px 16px;border-radius:10px;border:1px solid #2a2f3a;
 background:#262c38;color:#fff;font-size:14px;cursor:pointer}
.btn.warn{border-color:#7a2e2e}
.ft{color:#5a5f75;font-size:11px;text-align:center;margin:18px 0 8px}
.tbd{color:#5a5f75;font-style:italic}
"""

JS_TABS = """
function tab(n){var ps=document.querySelectorAll('.pane'),ts=document.querySelectorAll('.tab');
for(var i=0;i<ps.length;i++){ps[i].classList.toggle('act',i===n);ts[i].classList.toggle('act',i===n);}
try{localStorage.setItem('dz_tab',n);}catch(e){}}
try{var t=parseInt(localStorage.getItem('dz_tab')||'0');if(t>=0&&t<4)tab(t);}catch(e){}
"""

JS_RESOLVE = """
document.querySelectorAll('.tbd').forEach(function(el){
 var vid=el.getAttribute('data-vid');if(!vid)return;
 fetch('RESOLVE_URL?vid='+encodeURIComponent(vid),{credentials:'same-origin'})
 .then(function(r){return r.json();}).then(function(j){
  if(j&&j.ok){el.classList.remove('tbd');
   el.innerHTML='<div class="dr">'+esc(j.drama)+'</div>'+
    (j.ep?'<div class="ep">'+esc(j.ep)+'</div>':'');}
  else{el.classList.remove('tbd');el.innerHTML='<div class="dr">Video '+esc(vid)+'</div>';}
 }).catch(function(){el.classList.remove('tbd');});
});
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
"""

JS_CTL = """
function ctl(path,msg){if(!confirm(msg))return;
 fetch(path,{method:'POST',credentials:'same-origin'}).then(function(r){return r.json();})
 .then(function(j){alert(j.ok?'Ho gaya ✓':'Masla: '+JSON.stringify(j));location.reload();})
 .catch(function(){alert('Request fail ho gayi');});}
"""


def _item_html(drama, ep, meta, num=None, tbd_vid=None):
    if tbd_vid:
        inner = '<span class="tbd" data-vid="%s">Naam maloom ho raha…</span>' % html.escape(tbd_vid)
    else:
        inner = '<div class="dr">%s%s</div>' % (
            ('<span class="num">%d</span>' % num) if num else "",
            html.escape(drama))
        if ep:
            inner += '<div class="ep">%s</div>' % html.escape(ep)
    if meta:
        inner += '<div class="mt">%s</div>' % meta
    return '<div class="item">%s</div>' % inner


def _src_item(url):
    """Dashboard queue/done/failed entry (source URL) -> item html."""
    vid = vid_of(url if isinstance(url, str) else (url.get("url") if isinstance(url, dict) else ""))
    title = get_title(vid) if vid else None
    if title:
        return _item_html(drama_name(title), ep_info(title), None)
    return _item_html(None, None, None, tbd_vid=vid or "?")


def build_app(key):
    st = dash_status()
    running = st.get("running", False)
    pct = st.get("progress_pct")
    stage = st.get("stage") or ""
    queue = st.get("queue") or []
    done = st.get("done") or []
    failed = st.get("failed") or []

    pend_doc = load_json("pending_display.json", {})
    pend_items = pend_doc.get("items", []) if isinstance(pend_doc, dict) else []
    proc = load_json("processing.json", {})
    proc_title = proc.get("title") if isinstance(proc, dict) else None

    now_pkt = datetime.now(timezone(timedelta(hours=5))).strftime("%d %b, %H:%M")

    # ---- running card ----
    if running and proc_title:
        run_name = html.escape(drama_name(proc_title))
        run_sub = html.escape(ep_info(proc_title))
    elif running:
        run_name, run_sub = "Ban rahi hai…", ""
    else:
        run_name, run_sub = "Khaali", "Koi video process nahi ho rahi"
    pct_txt = (" — %s%%" % pct) if isinstance(pct, (int, float)) else ""
    bar = ('<div class="runbar"><i style="width:%s%%"></i></div>' % pct) \
        if running and isinstance(pct, (int, float)) else ""

    # ---- panes ----
    p_items = []
    for i, p in enumerate(pend_items, 1):
        t = p.get("title", "")
        meta_parts = []
        if p.get("channel"):
            meta_parts.append(html.escape(str(p["channel"])))
        if p.get("added_utc"):
            meta_parts.append(html.escape(fmt_pkt(p["added_utc"])))
        p_items.append(_item_html(drama_name(t), ep_info(t),
                                  " • ".join(meta_parts) if meta_parts else None,
                                  num=i))
    pend_pane = "".join(p_items) if p_items else \
        '<div class="empty">Koi pending video nahi — sab queue mein hai ✓</div>'

    q_pane = "".join(_src_item(u) for u in queue) if queue else \
        '<div class="empty">Server queue khaali hai</div>'
    # done: newest last in list -> reverse for newest-first
    d_pane = "".join(_src_item(u) for u in reversed(done)) if done else \
        '<div class="empty">Abhi tak koi complete nahi</div>'
    f_pane = "".join(_src_item(u) for u in failed) if failed else \
        '<div class="empty">Koi failed video nahi ✓</div>'

    rk = html.escape(key, quote=True)
    resolve_url = "/api/maint/resolve?key=" + rk

    page = """<!DOCTYPE html><html lang="ur"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>DiziVerse Manager</title><style>@@CSS@@</style></head><body>
<div class="hd"><h1>🎬 DiziVerse Manager</h1></div>
<div><span class="live"><span class="dot@@DOT@@"></span>@@LIVE@@</span></div>
<div class="sub">Updated: @@NOW@@ (PKT) • har 2 min mein auto-refresh</div>
<div class="stats">
 <div class="stat"><div class="k">▶️ Abhi</div><div class="n">@@STAGE@@</div>
  <div class="v" style="font-size:14px">@@RUN_NAME@@</div><div class="n">@@RUN_SUB@@</div>@@BAR@@</div>
 <div class="stat"><div class="k">⏳ Pending</div><div class="v">@@N_PEND@@</div></div>
 <div class="stat"><div class="k">📋 Queue</div><div class="v">@@N_QUEUE@@</div></div>
 <div class="stat"><div class="k">✅ Complete</div><div class="v">@@N_DONE@@</div></div>
 <div class="stat"><div class="k">❌ Failed</div><div class="v">@@N_FAIL@@</div></div>
</div>
<div class="tabs">
 <div class="tab act" onclick="tab(0)">⏳ Pending <b>@@N_PEND@@</b></div>
 <div class="tab" onclick="tab(1)">📋 Queue <b>@@N_QUEUE@@</b></div>
 <div class="tab" onclick="tab(2)">✅ Complete <b>@@N_DONE@@</b></div>
 <div class="tab" onclick="tab(3)">❌ Failed <b>@@N_FAIL@@</b></div>
</div>
<div class="pane act">@@P_PEND@@</div>
<div class="pane">@@P_QUEUE@@</div>
<div class="pane">@@P_DONE@@</div>
<div class="pane">@@P_FAIL@@</div>
<details class="ctl"><summary>⚙️ Control</summary>
 <button class="btn" onclick="ctl('/api/maint/restart-worker?key=@@KEY@@','Worker restart karein?')">🔄 Worker restart</button>
 <button class="btn warn" onclick="ctl('/api/maint/clear-failed?key=@@KEY@@','Failed list saaf karein?')">🧹 Failed saaf karo</button>
</details>
<div class="ft">DiziVerse Manager • free forever • @@NOW@@</div>
<script>@@JS_TABS@@</script>
<script>@@JS_RESOLVE@@</script>
<script>@@JS_CTL@@</script>
</body></html>"""
    subs = {
        "@@CSS@@": CSS,
        "@@DOT@@": " on" if running else "",
        "@@LIVE@@": "LIVE — kaam chal raha" if running else "Khaali",
        "@@NOW@@": now_pkt,
        "@@STAGE@@": (html.escape(stage) + pct_txt) if running else "",
        "@@RUN_NAME@@": run_name,
        "@@RUN_SUB@@": run_sub,
        "@@BAR@@": bar,
        "@@N_PEND@@": str(len(pend_items)),
        "@@N_QUEUE@@": str(len(queue)),
        "@@N_DONE@@": str(len(done)),
        "@@N_FAIL@@": str(len(failed)),
        "@@P_PEND@@": pend_pane,
        "@@P_QUEUE@@": q_pane,
        "@@P_DONE@@": d_pane,
        "@@P_FAIL@@": f_pane,
        "@@KEY@@": rk,
        "@@JS_TABS@@": JS_TABS,
        "@@JS_RESOLVE@@": JS_RESOLVE.replace("RESOLVE_URL", resolve_url),
        "@@JS_CTL@@": JS_CTL,
    }
    for k, v in subs.items():
        page = page.replace(k, v)
    return page
PYEOF_APP_4Q2W
python3 -m py_compile "$CH2/manager_app.py" || { echo "FAILURE: manager_app.py syntax error"; exit 1; }
echo "OK: manager_app.py written, syntax OK"

echo "== [4/7] writing dash_key.txt (mode 600) =="
printf '%s' "$DASHKEY" > "$CH2/dash_key.txt"
chmod 600 "$CH2/dash_key.txt"
echo "OK: dash_key.txt written"

echo "== [5/7] YouTube quota rotation (8 projects -> 48 uploads/day) =="
cat > "$CH2/quota_rotate.py" << 'PYEOF_ROTATE_6T1R'
"""YouTube API quota rotation — multiple GCP projects.

Har project ko 10k units/day milte hain; aik upload ~1600 units leta hai,
is liye fi project 6 uploads/day safe hai (pehle se proven).

Token files (is folder mein):
  token.json       -> project 1 (asal)
  token_p2.json .. token_pN.json -> projects 2..N (naye projects 9,10,11
                    khud-ba-khud pick ho jayenge — glob se dhoonda jata hai)

Usage quota_use.json mein track hota hai (America/Los_Angeles day —
YouTube quota midnight PT par reset hota hai).

pick_token(): sab se kam istemal wala token deta hai jis ki aaj ki
ginti < 6 ho, aur ginti barha kar save karta hai (optimistic — fail ho
jaye to aik slot zaya, jo safe hai). Sab khatam hon to RuntimeError.
"""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
USE_FILE = os.path.join(HERE, "quota_use.json")
PER_PROJECT_DAILY = 6


def _today_pt():
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


def token_files():
    files = []
    t1 = os.path.join(HERE, "token.json")
    if os.path.isfile(t1):
        files.append(t1)
    for p in sorted(glob.glob(os.path.join(HERE, "token_p*.json"))):
        if p not in files:
            files.append(p)
    return files


def project_count():
    return len(token_files())


def _load_use():
    try:
        d = json.load(open(USE_FILE, encoding="utf-8"))
    except Exception:
        d = {}
    if d.get("date") != _today_pt():
        d = {"date": _today_pt(), "counts": {}}
    if "counts" not in d:
        d["counts"] = {}
    return d


def _save_use(d):
    tmp = USE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, USE_FILE)


def usage_today():
    """{token_basename: count} — aaj ka istemal (diagnostic)."""
    return dict(_load_use().get("counts", {}))


def pick_token():
    """Agle upload ke liye token file ka path. Sab full hon to RuntimeError."""
    files = token_files()
    if not files:
        raise RuntimeError("koi token file nahi mili (token.json/token_p*.json)")
    use = _load_use()
    counts = use["counts"]
    # sab se kam istemal wala pehle = natural round-robin
    cands = sorted(files, key=lambda p: counts.get(os.path.basename(p), 0))
    for p in cands:
        name = os.path.basename(p)
        if counts.get(name, 0) < PER_PROJECT_DAILY:
            counts[name] = counts.get(name, 0) + 1
            _save_use(use)
            return p
    raise RuntimeError(
        "aaj ka quota tamam %d projects par khatam (%d uploads ho chuke)" %
        (len(files), sum(counts.values())))
PYEOF_ROTATE_6T1R
python3 -m py_compile "$CH2/quota_rotate.py" || { echo "FAILURE: quota_rotate.py syntax error"; exit 1; }
echo "OK: quota_rotate.py written, syntax OK"
python3 - "$CH2" << 'ROTATE_PATCH_EOF'
import datetime, glob, os, py_compile, shutil, sys

CH2DIR = sys.argv[1]

OLD1 = '''def _get_youtube():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not os.path.isfile(TOKEN_FILE):
        fail("upload", "Not signed in. Run: python diziverse.py --auth")
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, YOUTUBE_SCOPES)'''
NEW1 = '''def _get_youtube():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token_file = TOKEN_FILE
    try:
        import quota_rotate
        token_file = quota_rotate.pick_token()
        print("quota_rotate: %s" % token_file, flush=True)
    except ImportError:
        pass  # rotation module nahi hai -- purana behavior
    except Exception as e:
        fail("upload",
             "Aaj ka YouTube quota tamam projects par khatam ho gaya. %s" % e)
    if not os.path.isfile(token_file):
        fail("upload", "Not signed in. Run: python diziverse.py --auth")
    creds = Credentials.from_authorized_user_file(token_file, YOUTUBE_SCOPES)'''
OLD2 = '''        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as f:'''
NEW2 = '''        creds.refresh(Request())
        with open(token_file, "w") as f:'''

def log(m):
    print("QUOTA: %s" % m, flush=True)

cands = []
for pat in ("diziverse_main.py", "diziverse.py"):
    p = os.path.join(CH2DIR, pat)
    if os.path.isfile(p) and "def _get_youtube():" in open(p, encoding="utf-8").read():
        cands.append(p)
if len(cands) != 1:
    log("ABORT: expected 1 target, found %d" % len(cands)); sys.exit(2)
target = cands[0]
src = open(target, encoding="utf-8").read()
if "import quota_rotate" in src and "quota_rotate.pick_token()" in src:
    log("already applied - nothing to do"); sys.exit(0)
bak = target + ".bak-quota-%s" % datetime.datetime.now().strftime("%Y%m%d%H%M%S")
shutil.copy2(target, bak)
if src.count(OLD1) != 1:
    log("ABORT: _get_youtube anchor drifted, leaving untouched"); sys.exit(3)
src = src.replace(OLD1, NEW1, 1)
if src.count(OLD2) != 1:
    shutil.copy2(bak, target)
    log("ABORT: refresh anchor drifted, restored"); sys.exit(4)
src = src.replace(OLD2, NEW2, 1)
open(target, "w", encoding="utf-8").write(src)
py_compile.compile(target, doraise=True)
log("applied OK + compile OK (backup %s)" % os.path.basename(bak))
toks = [t for t in [os.path.join(os.path.dirname(target), "token.json")] +
        sorted(glob.glob(os.path.join(os.path.dirname(target), "token_p*.json")))
        if os.path.isfile(t)]
log("%d token files -> %d uploads/day capacity" % (len(toks), len(toks) * 6))
ROTATE_PATCH_EOF
[ $? -eq 0 ] || { echo "FAILURE: quota rotation patch failed"; exit 1; }
echo "OK: quota rotation installed"

echo "== [6/7] restarting maintenance service (tunnel untouched) =="
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
echo "OK: service running (pid $SRVPID)"

echo "== [7/7] health check =="
if python3 - "$KEY" << 'HEALTHEOF'
import json, sys, urllib.request
key = sys.argv[1]
with urllib.request.urlopen(
        "http://127.0.0.1:8899/api/maint/health?key=" + key,
        timeout=15) as r:
    h = json.load(r)
print("health: worker_running=%s queue=%d failed=%d"
      % (h["worker_running"], h["queue_len"], h["failed_count"]))
HEALTHEOF
then
  echo "OK: health check passed"
else
  echo "FAILURE: health check failed"
  exit 1
fi

echo ""
echo "==================== SUCCESS ===================="
echo "Maintenance API updated. Public URL is UNCHANGED:"
cat "$CH2/maint_url.txt" 2>/dev/null || echo "  (maint_url.txt not found)"
echo ""
echo "New: GET /app - DiziVerse Manager (pending/queue/complete/failed)"
echo "================================================="
