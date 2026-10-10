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
