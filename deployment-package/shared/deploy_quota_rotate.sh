#!/bin/bash
# YouTube quota rotation deploy (user 2026-09-27: "quota ko bound na kren").
# Har GCP project ka 10k units/day quota istemal ho: token.json (project 1)
# + token_p*.json (projects 2..N, naye khud-ba-khud). Fi project 6 uploads/day.
# Surgical: sirf _get_youtube() mein token pick hook lagta hai.
# Safe: aborts on drift, idempotent, backup + compile verified.
set -u
# Usage: bash deploy_quota_rotate.sh [channel_dir]
#   channel_dir default: ~/diziverse-server/ch2
#   Har channel ka apna quota pool hota hai (apne token.json + token_p*.json,
#   apna quota_use.json) — aik channel ki uploads dosre ka quota nahi khaatin.
CH_DIR="${1:-$HOME/diziverse-server/ch2}"
python3 - "$CH_DIR" <<'PYEOF'

import datetime
import glob
import os
import py_compile
import shutil
import sys

CH_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/diziverse-server/ch2")

QUOTA_ROTATE_SRC = '''"""YouTube API quota rotation — multiple GCP projects.

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
'''

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
        pass  # rotation module nahi hai — purana behavior
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
    print("QUOTA PATCH: %s" % m, flush=True)


d = CH_DIR
if not os.path.isdir(d):
    log("ABORT: %s nahi mila — sahi channel dir dein, masalan: "
        "bash deploy_quota_rotate.sh ~/diziverse-server/ch3" % d)
    sys.exit(2)

# 1) quota_rotate.py likho
qr_path = os.path.join(d, "quota_rotate.py")
with open(qr_path, "w", encoding="utf-8") as f:
    f.write(QUOTA_ROTATE_SRC)
py_compile.compile(qr_path, doraise=True)
log("quota_rotate.py written + compile OK")

# 2) token files gino
toks = [os.path.join(d, "token.json")] + sorted(glob.glob(os.path.join(d, "token_p*.json")))
toks = [t for t in toks if os.path.isfile(t)]
log("token files found: %d" % len(toks))
for t in toks:
    log("  - %s" % os.path.basename(t))
if not toks:
    log("ABORT: koi token file nahi mili")
    sys.exit(2)

# 3) diziverse.py target dhoondo
cands = []
for pat in ("diziverse_main.py", "diziverse.py"):
    p = os.path.join(d, pat)
    if os.path.isfile(p) and "def _get_youtube():" in open(p, encoding="utf-8").read():
        cands.append(p)
if len(cands) != 1:
    log("ABORT: expected 1 target, found %d (%s)" % (len(cands), cands))
    sys.exit(2)
target = cands[0]
log("target=%s" % target)
src = open(target, encoding="utf-8").read()

if "import quota_rotate" in src and "quota_rotate.pick_token()" in src:
    log("already applied - nothing to do")
    sys.exit(0)

bak = target + ".bak-quota-%s" % datetime.datetime.now().strftime("%Y%m%d%H%M%S")
shutil.copy2(target, bak)
log("backup=%s" % bak)
try:
    if src.count(OLD1) != 1:
        log("ABORT: _get_youtube anchor not found or ambiguous "
            "(count=%d) - server code drifted, leaving untouched" % src.count(OLD1))
        sys.exit(3)
    src = src.replace(OLD1, NEW1, 1)
    log("_get_youtube hook patched")
    if src.count(OLD2) != 1:
        log("ABORT: refresh write-back anchor not found or ambiguous "
            "(count=%d) - restored backup" % src.count(OLD2))
        shutil.copy2(bak, target)
        sys.exit(4)
    src = src.replace(OLD2, NEW2, 1)
    log("refresh write-back patched")
    open(target, "w", encoding="utf-8").write(src)
    py_compile.compile(target, doraise=True)
    log("applied OK + compile OK")
except SystemExit:
    raise
except Exception as e:
    shutil.copy2(bak, target)
    log("ABORT: %s - restored backup" % e)
    sys.exit(5)

# 4) dry-run: pick_token bina count jalaye
sys.path.insert(0, d)
import quota_rotate
log("dry-run: %d projects, per-project %d/day => %d uploads/day capacity" %
    (quota_rotate.project_count(), quota_rotate.PER_PROJECT_DAILY,
     quota_rotate.project_count() * quota_rotate.PER_PROJECT_DAILY))
log("DONE")

PYEOF
