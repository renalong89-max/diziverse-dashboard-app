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


def _vid_from_url(url):
    """youtu.be/VIDEOID ya youtube.com/watch?v=VIDEOID se ID nikalo."""
    try:
        u = str(url or "")
        if "youtu.be/" in u:
            return u.split("youtu.be/")[1].split("?")[0].split("/")[0].strip()
        if "v=" in u:
            return u.split("v=")[1].split("&")[0].strip()
    except Exception:
        pass
    return ""


def track_done_total(done_urls):
    """API ki done list (sirf aakhri 10) + persistent seen IDs = asal total.

    Har render par naye IDs ko seen_done_ids.json mein jama karta hai,
    taake Complete count live aur sahi rahe. Sirf Complete ke liye hai,
    Current/Queue/Failed ko nahi chhoota."""
    try:
        seen = load_json("seen_done_ids.json", [])
        seen_set = set(seen) if isinstance(seen, list) else set()
        changed = False
        for u in (done_urls or []):
            vid = _vid_from_url(u)
            if vid and vid not in seen_set:
                seen_set.add(vid)
                changed = True
        if changed:
            save_json_600("seen_done_ids.json", sorted(seen_set))
        return len(seen_set)
    except Exception:
        try:
            return len(done_urls or [])
        except Exception:
            return 0


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


def build_app(key, resolve_path="/api/maint/resolve", show_control=True):
    """key: dashboard/maint key. resolve_path: lazy title-resolve endpoint
    (None = disable). show_control: worker restart / clear-failed buttons."""
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
    resolve_url = (resolve_path + "?key=" + rk) if resolve_path else ""

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
@@CTL@@
<div class="ft">DiziVerse Manager • free forever • @@NOW@@</div>
<script>@@JS_TABS@@</script>
@@JS_RESOLVE_TAG@@
@@JS_CTL_TAG@@
</body></html>"""
    ctl_html = ""
    if show_control:
        ctl_html = """<details class="ctl"><summary>⚙️ Control</summary>
 <button class="btn" onclick="ctl('/api/maint/restart-worker?key=@@KEY@@','Worker restart karein?')">🔄 Worker restart</button>
 <button class="btn warn" onclick="ctl('/api/maint/clear-failed?key=@@KEY@@','Failed list saaf karein?')">🧹 Failed saaf karo</button>
</details>"""
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
        "@@N_DONE@@": str(track_done_total(done)),
        "@@N_FAIL@@": str(len(failed)),
        "@@P_PEND@@": pend_pane,
        "@@P_QUEUE@@": q_pane,
        "@@P_DONE@@": d_pane,
        "@@P_FAIL@@": f_pane,
        "@@KEY@@": rk,
        "@@CTL@@": ctl_html,
        "@@JS_TABS@@": JS_TABS,
        "@@JS_RESOLVE_TAG@@": ("<script>" + JS_RESOLVE.replace("RESOLVE_URL", resolve_url) + "</script>") if resolve_url else "",
        "@@JS_CTL_TAG@@": ("<script>" + JS_CTL + "</script>") if show_control else "",
    }
    for k, v in subs.items():
        page = page.replace(k, v)
    return page
