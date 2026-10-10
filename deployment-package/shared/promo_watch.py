#!/usr/bin/env python3
"""DiziVerse promo watcher v2 — naye promos/trailers khud dhoondh kar queue mein dalta hai.

User ke hukum (2026-09-26):
- 18 verified channels par nazar rakho (7 MAIN broadcaster/producer + 11 drama-name).
- PRIORITY: main channels pehle, phir drama-name channels (list order = scan/submit order).
- Jo bhi NAYA promo/trailer aaye (kisi naye drama ka bhi) us ka link khud uthao.
- SAME-PROMO DEDUP: ek hi promo (same drama + chapter/sezon no + trailer no) kayi
  channels par aata hai. Jab tak ek copy pipeline mein hai (queued/processing),
  baqi channels ki copies SKIP (reserved). Kamyab upload ke baad PERMANENT skip.
  Agar copy FAIL ho jaye to reservation khul jati hai — dusre channel ki copy
  agle run mein try ho sakti hai.
- Daily cap 6 (YouTube quota). Cap lagne par candidates PENDING queue mein rehte
  hain; agle run mein sab se pehle wahi process hote hain (koi promo zaya nahi).
- Title words aage-peeche ho sakte hain — gate order-independent hai.
- Hamesha PROMO target (fragman/tanitim/trailer/on izleme); ozet, kamera arkasi,
  backstage reject; 30s se chhota reject.
- Sirf TURKISH titles target — English titles (episode/trailer/season wale,
  Turkish marker ke baghair) SKIP, kabhi queue nahi (user 2026-09-26).
- Sirf DRAMA promos — game/daytime/reality shows (3'te 3, En Hamarat Benim,
  Gelin Evi, Survivor...) khud pehchan kar SKIP (user 2026-09-26).
- Server videos ko EK-EK karke sequential process karta hai — watcher sirf queue.

v3 (2026-09-26) izafay:
- Channel-affix guard: "| TRT1" / "TRT1 |" jaisay leading/trailing broadcaster
  affix parse se pehle hataye jate hain; bare channel naam (trt1, atv...)
  kabhi drama key nahi ban sakta.
- Review queue (promo_review.json): jahan drama yaqeen se na nikle, wahan
  item silent skip / ghalat queue nahi hota — review file mein jata hai
  (sirf storage; WhatsApp flow abhi nahi).
- Reconcile: submit ke baad server par kahin na milne wali videos (claim
  2h+ purana, ya idle server par sub-items) dobara pending mein dali jati
  hain — "seen mark ho gaya, submit fail" wala gap band.
- Quarantine rule: 30+ din purane items permanent drop; baqi naye gates se
  re-classify (dup/non-drama/english drop, valid pending mein wapas).
- Per-channel first-run baseline: naye channel ki pehli scan sirf seen
  record karti hai, queue nahi — purani videos "nayi" ban kar flood nahi
  kartin.

Sirf stdlib. yt-dlp binary: ~/.local/bin/yt-dlp
State files (same dir): promo_seen.json, promo_submitted.json,
  promo_watch_state.json, promo_claims.json, promo_done.json, promo_pending.json
Summary JSON stdout par — cron worker isi se user ko khabar deta hai.
"""

import json
import os
import re
import glob
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# 2026-10-02 (user order: SARY RASTY MULTIPLE): path health tracking.
# Primary fail ho to backup par shift; failed path "fixing" mein.
try:
    import path_manager
    _PATH_MGR = path_manager
except Exception:
    _PATH_MGR = None


def _path_record(name, ok, note=""):
    if _PATH_MGR:
        try:
            _PATH_MGR._record(name, ok, note)
        except Exception:
            pass

HERE = os.path.dirname(os.path.abspath(__file__))
GOAL_FILES = os.path.expanduser(
    "~/workspace/goals/youtube-channel-management/files/diziverse-dashboard-app")
URL_FILE = os.path.join(GOAL_FILES, "dashboard_url.txt")
YTDLP = os.path.expanduser("~/.local/bin/yt-dlp")

PLAYLIST_END = 20        # har channel ke itne latest videos dekho
QUOTA_ROTATION_LIVE = True  # 2026-09-27: server par quota rotation deploy ho gaya (update_maint.sh SUCCESS)
def _quota_projects():
    # quota_projects.txt mein asal project count hai; naye token_pN.json
    # (9, 10, 11...) judte hi ye file update hoti hai, cap khud barh jati hai.
    try:
        n = int(open(os.path.join(HERE, "quota_projects.txt"), encoding="utf-8").read().strip())
        return n if n >= 1 else 1
    except Exception:
        return 8
QUOTA_PROJECTS = _quota_projects() if QUOTA_ROTATION_LIVE else 1  # token.json + token_p*.json
DAILY_CAP = QUOTA_PROJECTS * 6  # fi project 6 uploads/day (~1600 units/upload, 10k/day)
MIN_DURATION = 30       # is se chhota = short/clip, promo nahi
MAX_DURATION = 170      # user 2026-10-02: promo 2:50 se lambi NAHI ho sakti (scene/episode clip nahi)
CHANNEL_TIMEOUT = 150
QUARANTINE_TTL_DAYS = 30  # quarantine mein itne din se zyada purana item = permanent drop
REVIEW_CAP = 200          # promo_review.json mein max itni entries

PROMO_WORDS = ("fragman", "tanitim", "trailer", "on izleme")
BAD_WORDS = ("ozet", "ozt", "kamera arkasi", "backstage", "kuliss")
# broadcaster/channel words jo title se nikal kar drama pehchante hain
CHAN_WORDS = ("trt1", "trt", "kanald", "kanal", "atv", "showtv", "show",
              "startv", "star", "nowtv", "now", "tv", "dizi", "dizisi",
              "resmi", "official", "youtube")

# Bare channel/broadcaster naam — ye KABHI drama key nahi ban sakte.
# (TRT1 bug: "Kod Adı Kırlangıç 87. Bölüm 1. Fragmanı | TRT1" ka drama
# "TRT1" parse ho gaya tha. Pehla hissa _strip_channel_affix rokta hai,
# ye set aakhri hifazat hai — drama in mein se hua to use kharij karo.)
CHANNEL_NAMES = frozenset((
    "trt1", "trt", "trtspor", "trtmuzik",
    "atv", "kanald", "kanal", "showtv", "show",
    "startv", "star", "nowtv", "now", "tv8", "tv",
    "dizi", "dizisi", "resmi", "official", "youtube",
))

# "| <channel>" / "<channel> |" affix — pipe ke us paar sirf ye bare
# identifier ho to poora segment parse se pehle hata do.
CHANNEL_AFFIXES = frozenset((
    "trt1", "trt", "atv", "kanald", "showtv", "startv", "nowtv", "tv8",
))

# English title -> canonical drama key. Broadcaster apne international channel
# par wohi promo English naam se dalte hain (tasdeeq-shuda jode):
#   One More Chance = Kizilcik Serbeti | Marriage Is Beautiful = Evlilik Guzeldir
#   Love and Throne = Ask ve Taht | Your Love is a Fire = Sevdan Bir Ates
#   The Lives of Others = Baskalarinin Hayati | Bride House = Gelin Evi
# NOTE (user 2026-09-26): sirf Turkish target — English titles ab QUEUE nahi
# hote (is_english_title gate). Ye ALIASES sirf DEDUP ke liye rakhe hain:
# pehle upload ho chuki English copies ke muqable mein nayi Turkish copy
# aaye to duplicate pehchani jaye.
ALIASES = {
    "onemorechance": "kizilcikserbeti",
    "marriageisbeautiful": "evlilikguzeldir",
    "loveandthrone": "askvetaht",
    "yourloveisafire": "sevdanbirates",
    "yourloveisfire": "sevdanbirates",
    "thelivesofothers": "baskalarininhayati",
    "bridehouse": "gelinevi",
    # 2026-10-02: TRT1 English copy "This Sea Will Overflow Episode 33
    # Trailer" duplicate upload ho gaya tha (UJBrzAf4c88) — Turkish
    # "Taşacak Bu Deniz 33. Bölüm Fragmanı" (0Y1JfJclGD4) pehle upload ho
    # chuki thi. Dono ab dedup mein ek hi drama mane jayenge.
    "thisseawilloverflow": "tasacakbudeniz",
}


def _dequote(t):
    # Title ke andar quote wala hissa (dialogue) nikal do — har style:
    # curly/straight double quotes, German-style, guillemets.
    # Unicode escapes istemal kiye hain taake koi character ghum na ho.
    # (Seedha ' apostrophe ko nahi chhedate — wo lafzon ka hissa hai.)
    t = re.sub(r'[“”„«‹][^“”„«»‹›]*[“”„»›]', " ", t)
    t = re.sub(r'"[^"]*"', " ", t)
    return t


def log(msg):
    print("[promo_watch] %s" % msg, flush=True)


def _norm(s):
    import unicodedata
    # NFD (decomposed: u + combining breve) -> NFC taake ü/ö/ş/ğ sahi milen
    s = unicodedata.normalize("NFC", s or "")
    # invisible bidi/control chars hatao (YouTube titles mein \u202a waghera aata hai)
    s = re.sub(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f\ufeff\u200b]", "", s)
    # capital İ (U+0130) -> i : .lower() isay "i + combining dot" bana deta hai
    s = s.replace("İ", "i").replace("I", "i")
    s = s.lower()
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"), ("ü", "u"),
                 ("ö", "o"), ("ç", "c")):
        s = s.replace(a, b)
    return s


def is_promo(title, duration):
    t = _norm(title)
    if not any(w in t for w in PROMO_WORDS):
        return False
    if any(w in t for w in BAD_WORDS):
        return False
    try:
        if duration:
            d = int(duration)
            if d < MIN_DURATION:
                return False
            # user 2026-10-02: 2:50 se lambi video promo NAHI (scene/clip hai)
            if d > MAX_DURATION:
                return False
    except (TypeError, ValueError):
        pass
    return True


ENGLISH_MARKERS = ("episode", "episodes", "trailer", "season", "preview")
TURKISH_MARKERS = ("bolum", "fragman", "sezon", "tanitim", "izleme")


def is_english_title(title):
    """User ka hukum (2026-09-26): sirf TURKISH target, English nahi.
    English promo markers (episode/trailer/season) hon aur koi Turkish
    marker (bölüm/fragman/sezon/tanıtım/izleme) na ho -> English copy,
    SKIP. Dono hon (mixed) to Turkish mana jata hai — conservative."""
    t = _norm(title)
    if not any(w in t for w in ENGLISH_MARKERS):
        return False
    return not any(w in t for w in TURKISH_MARKERS)


# Game / daytime / reality shows — ye DRAMA nahi hain. Inke "fragman" ko
# kabhi queue nahi karna (user 2026-09-26: har link khud parkho — drama
# promo hai ya nahi). Normalized title par substring match. Koi naya
# non-drama show nazar aaye to pattern yahin add karo.
NON_DRAMA_SHOWS = (
    "3'te 3",            # TRT1 game show
    "en hamarat benim",  # daytime cooking competition
    "gelin evi",         # daytime show
    "gelinim mutfakta",  # daytime cooking show (Kanal D daily) — caught 2026-09-28
    "bride house",       # Gelin Evi ki English copy
    "esra erol",         # daytime
    "muge anli",         # daytime (Müge Anlı)
    "survivor",          # reality competition
    "masterchef",        # reality competition
    "o ses turkiye",     # reality (O Ses Türkiye)
    "kim milyoner",      # game show (Kim Milyoner Olmak İster)
    "arda ile omuz omuza",  # celebrity cooking show (Kanal D)
    "arda'nin mutfagi",    # cooking show (Arda'nın Mutfağı, Kanal D daily) — caught 2026-09-29
    "beyaz'la joker",      # entertainment/show (Kanal D) — user 2026-10-01: drama nahi, SKIP
    "konustukca",          # daytime/lifestyle program (Kanal D, Özlem Yıldız & Arzu Öztürk) — drama nahi, SKIP (2026-10-02)
    "pazar gezmesi",       # Sunday daytime/lifestyle program (Kanal D, Asiye Acar) — drama nahi, SKIP (2026-10-03)
    "ali ihsan varol ile kumbara",  # quiz/game show (NOW, Fabrika Yapım) — drama nahi, SKIP (user 2026-10-05)
    "kumbara",            # "Ali İhsan Varol ile Kumbara" quiz show — short-name guard (2026-10-05)
)


# Cooking/food program keywords — DRAMA NAAM par lagte hain (2026-09-29:
# "Arda'nın Mutfağı" slip ke baad user order — queue se pehle drama-naam
# check lazmi). Koi Turkish drama ka naam in words se milta-julta nahi.
NON_DRAMA_KEYWORDS = (
    "mutfa",      # mutfak / mutfağı / mutfağında — cooking shows
    "yemek",      # yemek programları/tarifleri
    "tarif",      # tarifler (cooking)
    "asci",       # aşçı (chef)
    "chef",       # chef shows
    "restoran",
    "lokanta",
    "pastane",
    "podcast",      # podcast shows — drama nahi (user 2026-10-01)
)


def is_non_drama(title, drama=""):
    """Game/daytime/reality/cooking show ka promo -> True (drama nahi, SKIP).
    Sirf title nahi, DRAMA KA NAAM bhi check hota hai (2026-09-29 user order):
    "Arda'nın Mutfağı 15. Bölüm Fragmanı" title mein fragman tha is liye
    purana title-only check usay pakar nahi saka tha. Drama naam mein cooking
    keyword mile to bhi SKIP."""
    t = _norm(title)
    d = _norm(drama or "")
    for w in NON_DRAMA_SHOWS:
        if w in t or w in d:
            return True
    for k in NON_DRAMA_KEYWORDS:
        if k in t or k in d:
            return True
    return False


# Sponsored/branded promo copies (user 2026-09-28: "ETi Browni koi drama nahi")
# Ye brand ke ads hote hain, saaf drama promo nahi — SKIP karo.
BRANDED_SPONSORS = (
    "eti browni",
    "eti ",
    "ulker",
    "coca cola",
    "pepsi",
    "turkcell",
    "vodafone",
    "turk telekom",
)


def is_branded(title):
    """Sponsored/branded promo copy -> True (SKIP)."""
    t = _norm(title)
    return any(w in t for w in BRANDED_SPONSORS)


def _strip_words(t, words):
    for w in words:
        t = re.sub(r"\b%s\b" % re.escape(w), " ", t)
    return t


def _frag_kind(word):
    w = word or ""
    if w.startswith("fragman"):
        return "fragman"
    if w.startswith("tanitim"):
        return "tanitim"
    if w.startswith("trailer"):
        return "trailer"
    return "izleme"


def _looks_like_affix(seg):
    """Bare channel-tag jaisa pipe segment: chhota, bina digits, bina promo
    words. (t pehle se normalized hota hai.)"""
    w = seg.split()
    if not w or len(w) > 3:
        return False
    if re.search(r"\d", seg):
        return False
    if re.search(r"fragman|tanitim|trailer|izleme|bolum|sezon|season|"
                 r"episode|preview", seg):
        return False
    return True


def _strip_channel_affix(t):
    """Leading/trailing "| <channel>" affix hatao — parse se pehle.

    Sirf tab jab pipe ke us paar BARE channel identifier ho (koi aur lafz
    na ho): "kod adi kirlangic 87. bolum | trt1" -> "kod adi kirlangic
    87. bolum"; "trt1 | kod adi ..." -> "kod adi ...". "@TRT1" handle bhi
    pakda jata hai. Drama-name handles ("@haysiyetkanald") ko nahi
    chhedate — wo bare identifier nahi hain.

    GENERALIZED (v4): two-tier. Tier 1 (CERTAIN_AFFIXES: hardcoded + learned)
    unconditional strip — ye kabhi drama nahi. Tier 2 (DYNAMIC_AFFIXES:
    watch_channels names/handles, jin mein drama-naam bhi hain) SIRF tab
    strip jab doosri taraf drama words hon — "Haysiyet | 4. Bölüm" ka head
    kabhi na kate. Unknown affix-jaisa segment mile to AFFIX-SUSPECT log
    hota hai (strip nahi — ghalat drama ka naam katne se behtar hai ke
    review mein jaye).
    """
    parts = t.split("|")
    if len(parts) > 1:
        tail = parts[-1].strip().lstrip("@")
        core = re.sub(r"[^a-z0-9]", "", tail)
        rest = "|".join(parts[:-1])
        if core in CERTAIN_AFFIXES or (
                core in DYNAMIC_AFFIXES and _has_drama_words(rest)):
            parts = parts[:-1]
            t = "|".join(parts)
        elif core not in DYNAMIC_AFFIXES and _looks_like_affix(tail):
            log("AFFIX-SUSPECT (tail): '%s' — naya channel affix ho sakta hai; "
                "channel_affixes_learned.json mein add karo" % tail[:40])
    parts = t.split("|")
    if len(parts) > 1:
        head = parts[0].strip().lstrip("@")
        core = re.sub(r"[^a-z0-9]", "", head)
        rest = "|".join(parts[1:])
        # FIX 2026-10-01 (TRT1 parsing): agar head khud ek known drama ka naam
        # hai (channel_drama_list.json mein), to use kabhi mat kato — ye drama
        # hai, channel affix nahi. ("Kod Adı Kırlangıç | 87. Bölüm" ka head
        # katne se drama khaali ho jata tha.)
        _head_is_drama = False
        try:
            _drama_list = load_json(os.path.join(HERE, "channel_drama_list.json"), {}).get("dramas", [])
            _head_norm = re.sub(r"[^a-z0-9]", "", _norm(head))
            for _d in _drama_list:
                if re.sub(r"[^a-z0-9]", "", _norm(_d)) == _head_norm:
                    _head_is_drama = True
                    break
        except Exception:
            pass
        if head and not _head_is_drama and (core in CERTAIN_AFFIXES or (
                core in DYNAMIC_AFFIXES and _has_drama_words(rest))):
            t = "|".join(parts[1:])
        elif (head and not _head_is_drama and core not in DYNAMIC_AFFIXES
                and _looks_like_affix(head)
                and _rest_has_full_drama(rest)):
            # unknown channel head ("YENIKANAL | Halef 37. Bölüm ..."):
            # drama doosri taraf mehfooz hai -> head kato
            t = "|".join(parts[1:])
        elif (head and not _head_is_drama and core not in DYNAMIC_AFFIXES
                and _looks_like_affix(head)):
            log("AFFIX-SUSPECT (head): '%s' — naya channel affix ho sakta hai; "
                "channel_affixes_learned.json mein add karo" % head[:40])
    return t.strip()


def parse_combo(title):
    """Title -> (combo_key|None, sig, drama). Order-independent.

    combo_key = 'drama|ep_kind|ep_no|frag_kind|frag_no' (solid: drama>=3 chars
    aur koi number maujood). sig = broadcaster-words-nikala normalized title
    (kamzor/new-drama titles ke liye backup).
    Numbers lafz se pehle bhi ho sakte hain ("2. Fragman") ya baad mein
    ("Fragman 2", "Season 2", "Episode 141") — dono samjhe jate hain.
    Quote ("...") ka hissa drama se nikal diya jata hai taake same promo ke
    mukhtalif-quote versions ek hi key banayen. English drama naam ALIASES
    se canonical Turkish key par map hote hain. Leading/trailing bare
    channel affix ("| TRT1", "TRT1 |") pehle hi hataya jata hai, aur bare
    channel naam kabhi drama nahi banta (TRT1 bug fix).
    """
    t = _dequote(_norm(title))
    t = _strip_channel_affix(t)
    # "| description" wala suffix hatao — pipe ABHI maujood hai, is liye ye
    # block pipe->space se PEHLE ana chahiye (2026-10-02 fix: TRT1 wali
    # pipe->space line isay dead kar rahi thi — drama key mein descriptor
    # lafz ghus rahe thay, e.g. "haysiyetsadikkamyonabindive").
    # Sirf tab jab pehle hisse mein poori drama info ho (number + promo word
    # + drama lafz); warna drama baad wale hisse mein ho sakta hai
    # ("Daha 17 | 18. Bölüm ...", "4. Bölüm | Haysiyet") to poora rehne do.
    # Drama-naam wala channel tail mein ho to bhi poora rakho.
    parts = t.split("|")
    if (len(parts) > 1 and re.search(r"\d", parts[0])
            and re.search(r"fragman|tanitim|trailer|izleme|bolum|sezon|season|"
                          r"episode|preview", parts[0])):
        tail_core = re.sub(r"[^a-z0-9]", "", parts[-1].strip().lstrip("@"))
        if _has_drama_words(parts[0]) or tail_core not in DYNAMIC_AFFIXES:
            t = parts[0]
    # FIX 2026-10-01 (TRT1 parsing): bachi hui pipe (|) separators ko space
    # se badlo taake "Drama | 87. Bölüm | TRT1" format sahi parse ho.
    t = t.replace("|", " ")
    # " - descriptor" wala suffix (2026-10-03: "Pazar Gezmesi 43. Bölüm
    # Fragmanı - Nuri Alço" se seekh — dash ke baad guest/description ata hai).
    # Pipe wali hi guard: pehle hisse mein number + promo word ho tabhi kato.
    dparts = t.split(" - ")
    if (len(dparts) > 1 and re.search(r"\d", dparts[0])
            and re.search(r"fragman|tanitim|trailer|izleme|bolum|sezon|season|"
                          r"episode|preview", dparts[0])):
        dtail = re.sub(r"[^a-z0-9]", "", dparts[-1].strip().lstrip("@"))
        if _has_drama_words(dparts[0]) or dtail not in DYNAMIC_AFFIXES:
            t = dparts[0]
    w = t  # working copy — mile hue hisse nikalte jao
    ep_no = frag_no = None
    ep_kind = frag_kind = ""

    # ep: "186. Bölüm" / "2. Sezon" / "Episode 141" / "Season 2"
    m = re.search(r"(\d{1,4})\s*\.?\s*(sezon|season|bolum|episode)\w*", w)
    if m:
        ep_no = m.group(1)
        ep_kind = "sezon" if m.group(2).startswith("se") else "bolum"
        w = w[:m.start()] + " " + w[m.end():]
    else:
        m = re.search(r"(sezon|season|bolum|episode)\w*\s*\.?\s*(\d{1,4})", w)
        if m:
            ep_kind = "sezon" if m.group(1).startswith("se") else "bolum"
            ep_no = m.group(2)
            w = w[:m.start()] + " " + w[m.end():]

    # frag: "2. Fragman" ya "Fragman 2" ya "Trailer 1" ya bina number "Tanıtım"
    m = re.search(r"(\d{1,4})\s*\.?\s*(fragman|tanitim|trailer|on\s*izleme|izleme)\w*", w)
    if m:
        frag_no = m.group(1)
        frag_kind = _frag_kind(m.group(2))
        w = w[:m.start()] + " " + w[m.end():]
    else:
        m = re.search(r"(fragman|tanitim|trailer|on\s*izleme|izleme)\w*\s*\.?\s*(\d{1,4})", w)
        if m:
            frag_kind = _frag_kind(m.group(1))
            frag_no = m.group(2)
            w = w[:m.start()] + " " + w[m.end():]
        else:
            m = re.search(r"(fragman|tanitim|trailer|on\s*izleme|izleme)\w*", w)
            if m:
                frag_kind = _frag_kind(m.group(1))
                w = w[:m.start()] + " " + w[m.end():]

    # drama: bache hue text se @handles, promo words, channel words nikalo
    d = re.sub(r"@\w+", " ", w)
    d = _strip_words(d, ("fragman", "tanitim", "trailer", "izleme", "bolum",
                         "sezon", "season", "episode", "episodes", "yeni",
                         "preview", "final", "finale")
                     + CHAN_WORDS)
    d = re.sub(r"(?<!\.)\b\w\b(?!\.)", " ", d)  # akele reh jane wale harf ("Kanal D" ka D) — FIX 2026-10-01: dotted abbreviations ("A.B.İ.") ke letters mat urao
    drama = re.sub(r"[^a-z0-9]", "", d)
    if len(drama) < 3:
        # handle mein drama ka naam chhupa ho (@HaysiyetKANALD -> haysiyet)
        # FIX 2026-10-01: broadcaster handles (@atvturkiye, @trt1) ko drama mat
        # banao — ye channel hain, drama nahi.
        for h in re.findall(r"@(\w+)", t):
            core = re.sub(r"(trt|kanald|kanal|dizi|dizisi|tv|resmi|official)$", "", h)
            # Broadcaster handle skip karo
            _is_broadcaster = any(core.startswith(b) for b in ("atv", "trt", "kanald", "showtv", "startv", "nowtv", "tv8"))
            if _is_broadcaster:
                continue
            if len(core) >= 3:
                drama = core
                break
    drama = ALIASES.get(drama, drama)
    if drama in CHANNEL_NAMES:
        # Bare channel naam drama nahi ho sakta (TRT1 bug ki aakhri rok).
        drama = ""

    combo = None
    if len(drama) >= 3 and (ep_no or frag_no):
        combo = "%s|%s|%s|%s|%s" % (drama, ep_kind, ep_no or "",
                                   frag_kind, frag_no or "")

    s = _dequote(re.sub(r"@\w+", " ", t))
    s = _strip_words(s, CHAN_WORDS)
    for a, b in (("fragmani", "fragman"), ("tanitimi", "tanitim"),
                 ("bolumu", "bolum"), ("sezonu", "sezon")):
        s = re.sub(r"\b%s\b" % a, b, s)
    s = re.sub(r"(?<!\.)\b\w\b(?!\.)", " ", s)
    sig = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()
    if len(sig) < 4:
        sig = ""
    return combo, sig, drama


def claim_key(combo, sig):
    if combo:
        return "c:" + combo
    if sig:
        return "s:" + sig
    return None


def _key_fields(key):
    """'c:drama|epkind|epno|fragkind|fragno' -> (drama, epkind, epno, fragno).
    frag_kind ko nazar-andaz karte hain: fragman/trailer/tanitim/onizleme
    sab 'promo' hi hain — number farq batata hai."""
    if not key or not key.startswith("c:"):
        return None
    parts = key[2:].split("|")
    if len(parts) != 5:
        return None
    drama, ep_kind, ep_no, _fk, frag_no = parts
    return (drama, ep_kind, ep_no, frag_no)


def _compat(a, b):
    """Do promo keys equivalent hain? drama+chapter same hon, aur trailer
    number barabar ho.
    2026-10-02 FIX: bina-number wala sirf "1" ke barabar hai (pehla promo).
    "Teşkilat 187. Bölüm Fragmanı" (no number) aur "187. Bölüm 2. Fragmanı"
    ALAG promos hain — purana logic (kisi ek taraf missing ho to compatible)
    2nd fragman ko ghalat "upload ho chuka" keh kar skip kar deta tha."""
    if a[0] != b[0] or a[1] != b[1] or a[2] != b[2]:
        return False
    fa, fb = a[3], b[3]
    if fa and fb:
        return fa == fb
    # Ek taraf number missing: sirf "1"/missing ke barabar (pehla promo).
    # "2", "3" waghera alag promo hai — kabhi equivalent nahi.
    other = fa or fb
    return other in ("", "1")


def find_dup(combo, sig, done_map, claims):
    """'done' | 'inflight' | None. Exact key ke sath-sath equivalent
    promos bhi pakadta hai: mukhtalif quote, mukhtalif channel, ya
    Turkish/English title ka farq."""
    ck = claim_key(combo, sig)
    if ck:
        if ck in done_map:
            return "done"
        if ck in claims:
            return "inflight"
    if combo:
        want = _key_fields("c:" + combo)
        if want:
            for store, label in ((done_map, "done"), (claims, "inflight")):
                for k in store:
                    f = _key_fields(k)
                    if f and _compat(want, f):
                        return label
    return None


def _norm_dname(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").casefold())


def _fold_key(s):
    """2026-10-02: canonical drama key — _norm (Turkish vowel folding:
    ı→i, ş→s, ğ→g, ü→u, ö→o, ç→c) + alnum. known_folded/display_map isi se
    bante hain; resolve_drama bhi yehi istemal karega taake "Kuralsız
    Sokaklar" jese naam match hon ("kuralszsokaklar" vs "kuralsizsokaklar"
    mismatch na ho)."""
    return re.sub(r"[^a-z0-9]", "", _norm(s or ""))


def _nums_from_title(title):
    """(ep_no, frag_no) seedha title se — combo parse kabhi kabhi drama ka
    number (\"Daha 17\" ka 17) frag_no mein ghusa deta hai, is liye guard
    apne regex istemal karta hai."""
    t = (title or "").casefold()
    ep_no = ""
    m = re.search(r"(\d{1,4})\s*\.?\s*(bölüm|bolum|sezon|season|episode)", t)
    if m:
        ep_no = m.group(1)
    frag_no = ""
    m = re.search(r"(\d{1,4})\s*\.?\s*(fragman|tanıtım|tanitim|trailer)", t)
    if m:
        frag_no = m.group(1)
    else:
        m = re.search(r"(fragman|tanıtım|tanitim|trailer)\s*\.?\s*(\d{1,4})", t)
        if m:
            frag_no = m.group(2)
    return ep_no, frag_no


def resolve_drama(title, ch, known_dramas):
    """Candidate ka drama — fragile combo parse par bharosa kiye baghair.
    Pehle title ke shuru mein known drama naam (alias-channel fix 2026-10-02:
    drama-kind channel ka apna naam bhi channel-alias ho sakta hai — jese
    @behindtheveiltvseries jiska asal drama 'Gelin' hai; channel-name shortcut
    is case mein 'Behind the Veil' ko naya drama bana deta tha). Drama-channel
    ke liye uska apna exact naam fallback ke tor par. Keys _fold_key
    normalized hain (known_folded/display_map se consistent)."""
    head = _fold_key(title)[:60]
    for d in known_dramas:
        if d and d in head:
            return d
    if ch.get("kind") == "drama" and ch.get("name"):
        return _fold_key(ch["name"])
    _c, _s, drama = parse_combo(title or "")
    return drama or ""


def record_drama_name(display_name):
    """2026-10-02 (user order: "jo jo hota ja rha hy us ka record rakhty jaen"):
    jo drama process ho, us ka NAAM channel_drama_list.json mein record rakho
    taake new-drama detection dobara ghalat alert na de. Sirf saaf display
    naam add hota hai (koi parse-garbage nahi); pehle se mojood ho to skip.
    Best-effort — kabhi exception bahar nahi phenkta."""
    try:
        name = (display_name or "").strip()
        if len(name) < 3:
            return False
        p = os.path.join(HERE, "channel_drama_list.json")
        dl = load_json(p, {})
        if not isinstance(dl, dict):
            dl = {}
        dramas = dl.get("dramas", [])
        if not isinstance(dramas, list):
            dramas = []
        norm = _fold_key(name)
        if any(_fold_key(d) == norm for d in dramas if d):
            return False
        dramas.append(name)
        dl["dramas"] = dramas
        dl["built_utc"] = datetime.now(timezone.utc).isoformat()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dl, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        log("DRAMA-RECORDED: %s" % name)
        return True
    except Exception:
        return False


def pending_dup_action(pending, title, ch, known_dramas, chan_by_handle):
    """2026-09-28 (Daha-twice guard): kya YEHI promo (drama+episode+fragman)
    pehle se pending mein hai — file se aya ho ya isi run mein add hua ho?
    find_dup sirf done/claims dekhta tha, pending-vs-pending ka koi check
    nahi tha, isi liye aik hi scan mein do copies pending mein chali gayin
    (@kanald + @DahaOnYediDizisi, dono Daha 17 / 19. Bölüm / fragman).

    Drama resolve karne ke liye combo parse nahi (wo dialogue/quotes ko
    drama mein ghusa deta hai) — channel ka exact naam ya known drama list.

    Returns: ('skip', idx) | (None, None).
    2026-09-29 first-seen-wins: pending mein pehle se jo copy hai wahi jeetti
    hai — main/drama se farq nahi padta."""
    new_drama = resolve_drama(title, ch, known_dramas)
    new_ep, new_frag = _nums_from_title(title)
    if not new_drama or not new_ep:
        return (None, None)
    for i, p in enumerate(pending):
        if not isinstance(p, dict):
            continue
        pch = chan_by_handle.get(p.get("channel")) or {"kind": "main"}
        p_drama = resolve_drama(p.get("title", ""), pch, known_dramas)
        if p_drama != new_drama:
            continue
        p_ep, p_frag = _nums_from_title(p.get("title", ""))
        if p_ep != new_ep:
            continue
        if not ((not p_frag) or (not new_frag) or (p_frag == new_frag)):
            continue
        # 2026-09-29 first-seen-wins (user rule): pending mein pehle se jo
        # copy hai WAHI jeetti hai — channel type (main vs drama) se koi farq
        # nahi padta. Purana "main replaces drama" hata diya.
        return ("skip", i)
    return (None, None)


# ---------------------------------------------------------------------------
# 2026-09-29 NEW-DRAMA / NEW-SEASON detection (user order)
# Watcher "mature": koi NAYA drama aaye ya purane drama ka NAYA SEASON/chapter
# aaye to user ko khabar (WhatsApp + main chat). Sirf INFORM — list mein add
# user khud faisla kar ke batayega, watcher auto-add NAHI karta.
# State: new_drama_alerts.json (dedupe + notified_wa/notified_main flags),
#        drama_seasons.json (har drama ka max dekha hua sezon).
# ---------------------------------------------------------------------------
# Channel-jaisay lafz jo kabhi naya drama nahi ho sakte (sanity stoplist).
NOT_A_DRAMA = {"trt1", "atv", "showtv", "startv", "kanald", "nowtv",
               "nowtvturkiye", "atvturkiye", "trt", "kanal", "show", "star",
               " Fragman".strip().lower(), "tanitim", "fragman", "trailer",
               "bolum", "sezon", "dizi", "yakinda"}

VOWELS_FOLDED = set("aeiou")


def _quoted_launch_name(title):
    """Launch-teaser ka quoted drama naam: "Yeni Dizi 'Rüya' Yakında" -> 'Rüya'.
    Sirf bina-number launch pattern par — numbered promos mein quotes
    dialogue hote hain ("Kalbin Kadir'den..."), wahan nahi."""
    t = (title or "").split("|")[0]
    if not re.search(r"yakında|yakinda|yeni dizi|new series", t,
                     re.IGNORECASE):
        return ""
    if re.search(r"\d+\s*\.?\s*(bölüm|bolum|sezon|fragman|tanıtım|tanitim|"
                  r"trailer)", t, re.IGNORECASE):
        return ""
    m = re.search(r'"([^"]{2,60})"', t) or re.search(r"'([^']{2,60})'", t)
    return m.group(1).strip() if m else ""


def _drama_display(title):
    """Title se drama ka display naam nikalo: 'Güller ve Günahlar 1. Bölüm
    1. Fragmanı | Kanal D' -> 'Güller ve Günahlar'."""
    t = (title or "").split("|")[0]
    t = re.sub(r'"[^"]*"', " ", t)
    t = re.sub(r"'[^']*'", " ", t)
    t = _strip_channel_affix(t)
    # descriptor lafz hatao (drama naam nahi): "Yeni Dizi Rüya 1. Bölüm"
    # -> "Rüya 1. Bölüm"
    t = re.sub(r"(yeni dizi|new series|çok yakında|yakında|yakinda)", " ", t,
               flags=re.IGNORECASE)
    m = re.search(r"\d{1,4}\s*\.?\s*(bölüm|bolum|sezon|season|episode|"
                  r"fragman|tanıtım|tanitim|trailer)|"
                  r"(fragman|tanıtım|tanitim|trailer)", t, re.IGNORECASE)
    if m:
        t = t[:m.start()]
    t = re.sub(r"@\w+\s*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—:;,.!\"'")
    return t if len(t) >= 2 else ""


def _sane_new_drama(dkey):
    """Parse-garbage (channel naam, number-only, vowel-less) ko naya drama
    na samjho."""
    if not dkey or len(dkey) < 4:
        return False
    if dkey.isdigit():
        return False
    if dkey in NOT_A_DRAMA:
        return False
    if not (set(dkey) & VOWELS_FOLDED):
        return False
    return True


def detect_drama_events(title, ch, combo, sig, drama, known_folded,
                        display_map, alerts, seasons, now_iso):
    """Naya drama / naya season detect karo. alerts (list) aur seasons (dict)
    in-place update hote hain. Returns: is run mein naye bane alerts."""
    new_now = []
    dkey = drama or ""
    display = None
    if ch.get("kind") == "drama" and ch.get("name"):
        display = ch["name"].strip()
        if not dkey:
            dkey = re.sub(r"[^a-z0-9]", "", _norm(display))
    if not dkey:
        # sig-only (bina-number teaser): sirf quoted launch-naam par bharosa —
        # generic "yakında" teaser par ghalat alert nahi.
        disp = _quoted_launch_name(title)
        if disp:
            dkey = re.sub(r"[^a-z0-9]", "", _norm(disp))
            display = display or disp
    if not dkey:
        return new_now
    if dkey in known_folded:
        # Purana drama -> naya season/chapter?
        new_now += _detect_new_season(dkey, title, combo, alerts, seasons,
                                      display_map.get(dkey, dkey), now_iso)
        return new_now
    # Naya drama (user: sirf inform, list mein auto-add NAHI)
    if not _sane_new_drama(dkey):
        return new_now
    if any(a.get("drama_key") == dkey and a.get("kind") == "new_drama"
           for a in alerts):
        return new_now  # pehle bata diya
    display = display or _drama_display(title) or dkey
    rec = {"kind": "new_drama", "drama_key": dkey, "name": display,
           "season": None, "detected_utc": now_iso,
           "title": (title or "")[:80], "channel": ch.get("handle", ""),
           "notified_wa": False, "notified_main": False}
    alerts.append(rec)
    new_now.append(rec)
    log("NEW-DRAMA detect: %s (%s)" % (display, ch.get("handle", "")))
    return new_now


def _fetch_text(url, timeout=25):
    """URL fetch -> (text, final_url). Egress proxy ke TLS MITM ke liye
    runtime CA bundle istemal hota hai."""
    import ssl as _ssl
    _ctx = _ssl.create_default_context(
        cafile="/run/hatch/egress-tls/ca-bundle.pem")
    _req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36"})
    _resp = urllib.request.urlopen(_req, timeout=timeout, context=_ctx)
    return _resp.read().decode("utf-8", errors="ignore"), _resp.geturl()


def portion2_auto_eval(drama_name, new_alerts, now_iso):
    """2026-10-01 (user order: 3-portion system implement karo — "agar same
    naam hy tu kr do").
    Naya drama detect hote hi uske EXACT-naam + BLUE-TICK channel ki talash:
    YouTube search mein drama ke naam ka channelRenderer apne bounded segment
    mein khud ka verified badge rakhta ho -> portion-2 (kind=drama) mein add.
    Gates add_drama_channel.py wali: denylist, exact naam, tick — teeno yahin
    enforce hote hain. Sirf new-drama detection par chalta hai (rare), is liye
    scan par bojh nahi. Koi exception scan ko nahi rokegi.
    Returns status string; alert rec mein "portion2" field record hota hai."""
    rec = None
    for _a in new_alerts or []:
        if _a.get("kind") == "new_drama":
            rec = _a
            break
    if rec is None or not drama_name:
        return "skip"
    # 2026-10-01 (user: "pehchan rakhen drama ki aur show ki"): show/cooking/
    # podcast jaisa non-drama naam portion-2 mein KABHI nahi jayega — chahe
    # exact-naam + tick ho (Beyaz'la Joker seekh).
    if is_non_drama("", drama_name):
        rec["portion2"] = "non-drama-skip"
        log("PORTION-2 SKIP (show/non-drama, user rule): %s" % drama_name)
        return "non-drama-skip"
    try:
        _h, _ = _fetch_text(
            "https://www.youtube.com/results?search_query="
            + urllib.parse.quote_plus(drama_name + " dizi"))
        _starts = [m.start() for m in re.finditer(r'"channelRenderer":', _h)]
        _hit = None  # (status, channelId)
        for _idx, _s in enumerate(_starts):
            _e = _starts[_idx + 1] if _idx + 1 < len(_starts) else _s + 8000
            _seg = _h[_s:_e]
            _m = re.search(r'"title":\{"simpleText":"((?:[^"\\]|\\.)*)"\}', _seg)
            if not _m:
                continue
            try:
                _title = json.loads('"' + _m.group(1) + '"')
            except Exception:
                continue
            if " ".join(_title.split()).casefold() != \
               " ".join(drama_name.split()).casefold():
                continue
            _cm = re.search(r'"channelId":"(UC[^"]+)"', _seg)
            _cid = _cm.group(1) if _cm else None
            if "BADGE_STYLE_TYPE_VERIFIED" in _seg and _cid:
                _hit = ("verified", _cid)
            else:
                _hit = ("exact-no-tick", _cid)
            break
        if _hit is None:
            rec["portion2"] = "none"
            log("PORTION-2: %s — exact-naam+tick channel nahi mila" % drama_name)
            return "none"
        _status, _cid = _hit
        if _status != "verified":
            rec["portion2"] = "exact-no-tick"
            log("PORTION-2: %s — exact naam mila lekin tick nahi (user faisla)"
                % drama_name)
            return "exact-no-tick"
        # gates: denylist + pehle se watched
        _denied = set(load_json(os.path.join(HERE, "unverified_channels.json"),
                                {}).get("handles", []))
        _w = load_json(os.path.join(HERE, "watch_channels.json"), {})
        _chs = _w.get("channels", []) if isinstance(_w, dict) else []
        _ids = {c.get("handle") for c in _chs} | \
               {c.get("channel_id") for c in _chs}
        if _cid in _denied or _cid in _ids:
            rec["portion2"] = "denied-or-watched"
            return "denied-or-watched"
        # @handle resolve (na mile to channel-id form)
        _handle = None
        try:
            _ph, _final = _fetch_text(
                "https://www.youtube.com/channel/" + _cid)
            _hm = re.search(r'<link rel="canonical" '
                            r'href="https://www\.youtube\.com/(@[^"/]+)"', _ph)
            if _hm:
                _handle = urllib.parse.unquote(_hm.group(1))
            else:
                _hm = re.search(r"youtube\.com/(@[^/?#]+)", _final or "")
                if _hm:
                    _handle = urllib.parse.unquote(_hm.group(1))
        except Exception:
            _handle = None
        _ident = _handle or _cid
        if _ident in _denied or _ident in _ids:
            rec["portion2"] = "denied-or-watched"
            return "denied-or-watched"
        _entry = {"handle": _ident, "name": drama_name, "kind": "drama",
                  "verified": True, "verified_by": "auto-search",
                  "verified_utc": now_iso}
        if not _handle:
            _entry["channel_id"] = _cid
            _entry["url"] = "https://www.youtube.com/channel/" + _cid
        _chs.append(_entry)
        if isinstance(_w, dict):
            _w["channels"] = _chs
        else:
            _w = {"channels": _chs}
        save_json(os.path.join(HERE, "watch_channels.json"), _w)
        _v = load_json(os.path.join(HERE, "verified_channels.json"), [])
        _items = _v.get("channels", _v) if isinstance(_v, dict) else _v
        _ventry = dict(_entry)
        _ventry["source"] = "portion2-auto"
        _items.append(_ventry)
        save_json(os.path.join(HERE, "verified_channels.json"),
                  {"channels": _items}
                  if isinstance(_v, dict) and "channels" in _v else _items)
        rec["portion2"] = "added:" + _ident
        log("PORTION-2 ADD: %s -> %s (exact naam + tick, auto)" % (drama_name,
                                                                  _ident))
        return "added:" + _ident
    except Exception as _e:
        log("PORTION-2 eval fail (%s): %s" % (drama_name, str(_e)[:80]))
        return "eval-fail"


def _detect_new_season(dkey, title, combo, alerts, seasons, display_name,
                       now_iso):
    """Known drama ka naya sezon: numbered sezon increase, ya unnumbered
    'yeni sezon'. Pehli baar sirf record (alert nahi — pata nahi ke naya hai)."""
    out = []
    parts = (combo or "").split("|")
    if len(parts) >= 3 and parts[1] == "sezon" and parts[2].isdigit():
        sn = int(parts[2])
        prev = seasons.get(dkey)
        if prev is None:
            seasons[dkey] = sn
            return out
        if sn > prev:
            seasons[dkey] = sn
            if not any(a.get("drama_key") == dkey
                       and a.get("kind") == "new_season"
                       and a.get("season") == sn for a in alerts):
                rec = {"kind": "new_season", "drama_key": dkey,
                       "name": display_name, "season": sn,
                       "detected_utc": now_iso, "title": (title or "")[:80],
                       "channel": "", "notified_wa": False,
                       "notified_main": False}
                alerts.append(rec)
                out.append(rec)
                log("NEW-SEASON detect: %s %d. Sezon" % (display_name, sn))
        return out
    if "yeni sezon" in _norm(title):
        flag = dkey + ":yeni_sezon"
        if not seasons.get(flag):
            seasons[flag] = 1
            rec = {"kind": "new_season", "drama_key": dkey,
                   "name": display_name, "season": None,
                   "detected_utc": now_iso, "title": (title or "")[:80],
                   "channel": "", "notified_wa": False,
                   "notified_main": False}
            alerts.append(rec)
            out.append(rec)
            log("NEW-SEASON detect (yeni sezon): %s" % display_name)
    return out
# Jo promo copy PEHLE aaye (main verified channel ya same-drama-name channel)
# WAHI upload hogi; baad mein aane wali same-promo copy SKIP — sirf tab jab
# pehli ACTUALLY upload hui ho (done) ya pipeline mein ho (claims/pending).
# FALLBACK natural hai: pehli copy failed/quarantine mein ho (upload nahi hui)
# to done/claims/pending mein us ka record nahi hota -> nayi copy allow.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 2026-09-29 first-seen-wins duplicate policy (user rule)
# Jo promo copy PEHLE aaye (main verified channel ya same-drama-name channel)
# WAHI upload hogi; baad mein aane wali same-promo copy SKIP — sirf tab jab
# pehli ACTUALLY upload hui ho (done) ya pipeline mein ho (claims/pending).
# FALLBACK natural hai: pehli copy failed/quarantine mein ho (upload nahi hui)
# to done/claims/pending mein us ka record nahi hota -> nayi copy allow.
# ---------------------------------------------------------------------------
FUZZY_DUP_WINDOW_H = 48  # same promo ki doosri copy itne ghanton mein aaye


def _bolum_no(title):
    """Sirf 'N. Bölüm' wala number — sezon number nahi (false-positive guard)."""
    m = re.search(r"(\d{1,4})\s*\.?\s*(bölüm|bolum)", (title or "").casefold())
    return m.group(1) if m else ""


def promo_fuzzy_key(title, ch, known_dramas):
    """Cross-title same-promo key: (drama, frag_no). ep numbering ignore —
    '32. Bölüm 2. Fragmanı' aur '2. Sezon 2. Fragman' ek hi key banate hain
    (yehi Taşacak-duplicate ka root cause tha).
    FIX 2026-10-05 (Daha 17 20. Bölüm double-upload): bare 'Fragmanı' /
    'Tanıtımı' / 'Trailer' (bina number, jaise '20. Bölüm Fragmanı') ka
    frag_no khaali hota tha -> key None -> cross-source dedup kabhi fire
    nahi karta tha. Bina-number wala promo pehla (frag 1) hota hai, is liye
    promo-word mojood ho to default '1'."""
    drama = resolve_drama(title, ch or {}, known_dramas)
    _ep, frag = _nums_from_title(title)
    if not drama:
        return None
    if not frag:
        t = (title or "").casefold()
        if not re.search(r"(fragman|tanıtım|tanitim|trailer)", t):
            return None
        frag = "1"
    return (drama, frag)


def _rec_time(rec, keys):
    for k in keys:
        v = rec.get(k) if isinstance(rec, dict) else None
        if v:
            try:
                return datetime.fromisoformat(v)
            except Exception:
                pass
    return None


def fuzzy_first_wins(title, ch, known_dramas, done_map, claims, pending,
                     now, exclude_id=None):
    """'skip' | None. Kya isi promo (same drama + same frag_no, 48h window)
    ki pehle wali copy done/claims/pending mein pehle se hai?
    - bolum-number guard: dono titles mein alag 'N. Bölüm' ho to alag promos.
    - failed/quarantine ka record in stores mein nahi hota -> fallback: nayi
      copy allow (skip nahi)."""
    fk = promo_fuzzy_key(title, ch, known_dramas)
    if not fk:
        return None
    new_bolum = _bolum_no(title)
    for label, store, tkeys, is_dict in (
            ("done", done_map, ("done_utc",), True),
            ("inflight", claims, ("claimed_utc",), True),
            ("pending", pending, ("added_utc",), False)):
        recs = store.values() if is_dict else store
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            if exclude_id and rec.get("id") == exclude_id:
                continue
            rt = _rec_time(rec, tkeys)
            if rt is not None and (now - rt).total_seconds() > \
                    FUZZY_DUP_WINDOW_H * 3600:
                continue
            rtitle = rec.get("title", "")
            rb = _bolum_no(rtitle)
            if rb and new_bolum and rb != new_bolum:
                continue  # alag bölüm number = alag promo
            if promo_fuzzy_key(rtitle, None, known_dramas) == fk:
                return label
    return None


def migrate_state(claims, done_map, pending, now_iso):
    """Purani quote-polluted / baghair-alias keys ko naye canonical parse
    par lao; pending mein se done/inflight duplicates saaf karo.
    Idempotent hai — har run par chal sakta hai."""
    moved = {"claims": 0, "done": 0, "pending_dropped": []}

    def rekey(store, label):
        new_store = {}
        for old_key, rec in store.items():
            title = rec.get("title", "")
            combo, sig, _drama = parse_combo(title)
            new_key = claim_key(combo, sig) or old_key
            if new_key != old_key:
                moved[label] += 1
            if new_key in new_store:
                log("MERGE (%s): %s" % (label, title[:55]))
                continue
            new_store[new_key] = rec
        return new_store

    claims = rekey(claims, "claims")
    done_map = rekey(done_map, "done")

    kept = []
    for p in pending:
        title = p.get("title", "")
        combo, sig, _drama = parse_combo(title)
        p["combo"], p["sig"] = combo, sig
        dup = find_dup(combo, sig, done_map, claims)
        if dup:
            moved["pending_dropped"].append(
                {"id": p.get("id"), "title": title[:60], "why": dup})
            log("PENDING-DUP saaf (%s): %s" % (dup, title[:60]))
        else:
            kept.append(p)
    return claims, done_map, kept, moved


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # atomic: tmp ko asal file banao


# ---------------------------------------------------------------------------
# GENERALIZED channel-affix protection (v4) — "kal koi aur channel ho sakta hai"
# ---------------------------------------------------------------------------
# Two-tier:
#   Tier 1 (CERTAIN): hardcoded broadcasters (CHANNEL_AFFIXES) + learned file
#       (channel_affixes_learned.json — human-confirmed). Ye kabhi drama nahi
#       ho sakte -> pipe segment unconditional strip.
#   Tier 2 (DYNAMIC): watch_channels.json se names/handles. IN MEIN DRAMA KE
#       NAAM BHI HAIN ("Haysiyet", "Sevdan Bir Ateş", "Daha 17")! Is liye
#       conditional strip: sirf tab jab pipe ke doosri taraf drama words hon
#       ("Haysiyet | 4. Bölüm" mein head NA kate — drama mehfooz rahe).
#
# AHM: dynamic names CHANNEL_NAMES (never-drama guard) mein NAHI — warna
# drama-name channels ke promo parse hi na hon.
def _load_learned_affixes():
    s = set()
    try:
        learned = load_json(
            os.path.join(HERE, "channel_affixes_learned.json"), [])
        if isinstance(learned, list):
            for raw in learned:
                core = re.sub(r"[^a-z0-9]", "", _norm(str(raw)).lstrip("@"))
                if len(core) >= 2:
                    s.add(core)
    except Exception:
        pass
    return s


def _load_dynamic_affixes():
    s = set()
    try:
        data = load_json(os.path.join(HERE, "watch_channels.json"), {})
        for c in data.get("channels", []):
            if not isinstance(c, dict):
                continue
            for raw in (c.get("name", ""), c.get("handle", "")):
                core = re.sub(r"[^a-z0-9]", "", _norm(str(raw)).lstrip("@"))
                if len(core) >= 2:
                    s.add(core)
    except Exception:
        pass
    return s


CERTAIN_AFFIXES = CHANNEL_AFFIXES | _load_learned_affixes()
DYNAMIC_AFFIXES = _load_dynamic_affixes()
ALL_AFFIXES = CERTAIN_AFFIXES | DYNAMIC_AFFIXES

_PROMO_ROOTS = ("fragman", "tanitim", "trailer", "izleme", "bolum",
                "sezon", "season", "episode", "preview")
_STOPWORDS = frozenset("yeni dizi dizisi ve ile bir".split())


def _has_drama_words(t):
    """Kya text mein drama-naam jaisa koi lafz hai?
    Promo roots Turkish suffix ke sath bhi pehchane jate hain
    ("fragmani", "bolumu" waghera)."""
    for w in re.findall(r"[a-z]{3,}", t):
        if w in _STOPWORDS:
            continue
        if w.startswith(_PROMO_ROOTS):
            continue
        return True
    return False


def _rest_has_full_drama(t):
    """Kya baqi hisse mein mukammal drama info hai (number+promo+drama)?"""
    return (bool(re.search(r"\d", t))
            and bool(re.search(r"fragman|tanitim|trailer|izleme|bolum|sezon|"
                               r"season|episode|preview", t))
            and _has_drama_words(t))


def _drama_from_affix_segment(t, drama):
    """Kya parse shuda drama SIRF affix-jaisay pipe segment(s) se aaya hai?

    t: normalized title (pipe ke sath, _strip_channel_affix ke baad wala).
    Future ka UNKNOWN channel affix kabhi drama key na bane — aisa case
    review mein jaye, ghalat queue mein nahi.

    Safe hai kyunke: drama-first format ("Haysiyet | 4. Bölüm") mein drama
    pehle segment mein hota hai -> False. Jin segment mein digits/promo words
    hon wo affix-like nahi hote -> False. Drama agar hamare watch-channels
    mein se kisi ka naam hai (drama-name channel) to parse qabil-e-bharosa
    hai -> False. Sirf tab True jab drama aisi jagah se aaya ho jahan
    channel tag hota hai AUR wo naam unknown ho.
    """
    if not drama or "|" not in t:
        return False
    if drama in DYNAMIC_AFFIXES:
        return False  # known drama/channel name — parse bharosemand
    segs = t.split("|")
    first_core = re.sub(r"[^a-z0-9]", "", segs[0])
    if drama in first_core:
        return False  # drama-first format — safe
    found = [s for s in segs[1:]
             if drama in re.sub(r"[^a-z0-9]", "", s)]
    if not found:
        return False
    return all(_looks_like_affix(s) for s in found)
    os.replace(tmp, path)


def dashboard_base():
    """dashboard_url.txt se (tunnel, key). URL restart par badal jata hai — har run fresh."""
    with open(URL_FILE, encoding="utf-8") as f:
        base = f.read().strip().split()[0]
    key = urllib.parse.parse_qs(urllib.parse.urlparse(base).query).get("key", [""])[0]
    tunnel = base.split("/?key=")[0].split("?key=")[0]
    if not key or not tunnel.startswith("http"):
        raise RuntimeError("dashboard_url.txt mein sahi URL/key nahi mila")
    return tunnel, key


def api_status(tunnel, key):
    try:
        # Cloudflare kabhi-kabhi plain urllib ko block karta hai (403) —
        # is liye browser User-Agent lazmi hai.
        req = urllib.request.Request(
            tunnel + "/api/status?key=" + key,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        _path_record("dashboard_api", True)
        return data
    except Exception as e:
        log("api/status nahi mila: %s" % str(e)[:100])
        _path_record("dashboard_api", False, str(e))
        return {}


def _maint_base():
    """(maint_root, maint_key) live_url.txt se. Na mile to (None, None)."""
    try:
        live = os.path.join(os.path.expanduser("~"), "workspace",
            "goals/youtube-channel-management/hidden_files/maint_api",
            "live_url.txt")
        base = open(live, encoding="utf-8").read().strip().split()[0]
        if "?key=" not in base:
            return None, None
        root, mkey = base.split("?key=", 1)
        return root.rstrip("/"), mkey
    except Exception:
        return None, None


def _sync_dashboard_url_from_maint():
    """Maint API se live dashboard tunnel URL lo; dashboard_url.txt update karo.
    Tunnel restart par URL badal jata hai — ye sync recovery automatic banata
    hai. Returns (tunnel, key) ya None. Dashboard key hamesha purani rehti hai.
    """
    try:
        root, mkey = _maint_base()
        if not root:
            return None
        with urllib.request.urlopen(
                root + "/api/maint/dashboard-url?key=" + mkey,
                timeout=20) as r:
            url = (json.load(r).get("url") or "").strip()
        _path_record("maint_api", True)
        if not url.startswith("http"):
            return None
        old = open(URL_FILE, encoding="utf-8").read().strip()
        oldkey = urllib.parse.parse_qs(
            urllib.parse.urlparse(old).query).get("key", [""])[0]
        if not oldkey:
            return None
        new = url.rstrip("/") + "/?key=" + oldkey
        if new != old:
            tmp = URL_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new + "\n")
            os.replace(tmp, URL_FILE)
            log("dashboard URL auto-sync: %s" % url)
        return url.rstrip("/"), oldkey
    except Exception as e:
        log("dashboard-url sync fail: %s" % str(e)[:80])
        _path_record("maint_api", False, str(e))
        return None


def ensure_dashboard():
    """Kaam karta (tunnel, key) do. Tunnel restart hua ho to maint API se
    naya URL auto-sync karo. Sab fail ho to purana wapas (caller fail-closed).
    """
    tunnel, key = dashboard_base()
    if api_status(tunnel, key):
        return tunnel, key
    synced = _sync_dashboard_url_from_maint()
    if synced:
        t2, k2 = synced
        if api_status(t2, k2):
            log("dashboard naya URL par zinda hai")
        return t2, k2
    return tunnel, key


def vid_of(url):
    m = re.search(r"(?:youtu\.be/|v=)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def oembed_title(vid):
    try:
        with urllib.request.urlopen(
                "https://www.youtube.com/oembed?url=https://youtu.be/" + vid,
                timeout=15) as r:
            return json.load(r).get("title")
    except Exception:
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def submit_link(tunnel, key, video_id, ch="ch2"):
    """POST /submit. Returns: 'ok' | 'dup' | 'bad' | 'notoken' | 'unplayable' | 'error'."""
    link = "https://youtu.be/%s" % video_id
    data = urllib.parse.urlencode(
        {"key": key, "ch": ch, "link": link}).encode()
    req = urllib.request.Request(tunnel + "/submit", data=data, method="POST")
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=60) as r:
            loc = r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        # 303 ko NoRedirect rok deta hai -> HTTPError with headers
        loc = e.headers.get("Location", "") if e.headers else ""
        if not loc:
            _path_record("dashboard_api", False, "submit-no-location")
            return "error"
    except Exception as e:
        _path_record("dashboard_api", False, "submit:%s" % str(e)[:80])
        return "error"
    m = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("msg", [""])[0]
    # FIX 2026-09-29: dashboard ka playability gate naya msg=unplayable deta hai —
    # ye pehle "error" mein gir kar har run retry karta tha aur pending ko block
    # kar deta tha. Ab ye terminal outcome hai: server video pull nahi kar sakta
    # (embed Error 153 jaisi rok), retry kabhi nahi.
    _path_record("dashboard_api", True)
    return m if m in ("ok", "dup", "bad", "notoken", "unplayable") else "error"


def push_pending_display(pending):
    """POST pending list maint API ko taake /pending page live rahe.

    Best-effort: live_url.txt na mile ya tunnel down ho to silently skip.
    Kabhi exception bahar nahi phenkta.
    """
    try:
        live = os.path.join(
            os.path.expanduser("~"), "workspace",
            "goals/youtube-channel-management/hidden_files/maint_api",
            "live_url.txt")
        base = open(live, encoding="utf-8").read().strip()
        if "?key=" not in base:
            return
        root, key = base.split("?key=", 1)
        url = root.rstrip("/") + "/api/maint/deploy-pending?key=" + key
        items = [{"title": p.get("title", ""),
                  "channel": p.get("channel", ""),
                  "added_utc": p.get("added_utc", "")}
                 for p in pending if isinstance(p, dict)]
        data = json.dumps({"items": items}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception:
        pass


def push_pending_github(pending):
    """2026-10-02: pending list GitHub repo mein (pending.json) taake Android
    app ise seedha parh sake. Watcher har run ke baad call karta hai.
    Best-effort: git/push fail ho to silently skip, kabhi exception nahi."""
    try:
        repo = os.path.join(os.path.expanduser("~"), "workspace",
                            "diziverse-app")
        pj = os.path.join(repo, "pending.json")
        items = [{"title": p.get("title", ""),
                  "channel": p.get("channel", ""),
                  "added_utc": p.get("added_utc", "")}
                 for p in pending if isinstance(p, dict)
                 if p.get("title")]
        doc = {"updated_utc": datetime.now(timezone.utc).isoformat(),
               "items": items}
        new_body = json.dumps(doc, ensure_ascii=False, indent=1)
        try:
            old_body = open(pj, encoding="utf-8").read()
        except Exception:
            old_body = None
        if old_body == new_body:
            return  # koi tabdeeli nahi — push ki zaroorat nahi
        with open(pj, "w", encoding="utf-8") as f:
            f.write(new_body)
        # sirf pending.json commit karo (baqi untracked/modified files ko chhero mat)
        r1 = subprocess.run(
            ["git", "add", "pending.json"], cwd=repo,
            capture_output=True, timeout=30)
        if r1.returncode != 0:
            return
        r2 = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=repo,
            capture_output=True, timeout=30)
        if r2.returncode == 0:
            return  # staged changes nahi — kuch push nahi karna
        r3 = subprocess.run(
            ["git", "-c", "user.name=watcher", "-c",
             "user.email=watcher@local", "commit", "-m",
             "watcher: pending list update"],
            cwd=repo, capture_output=True, timeout=30)
        if r3.returncode != 0:
            return
        subprocess.run(["git", "push", "origin", "main"], cwd=repo,
                       capture_output=True, timeout=60)
    except Exception:
        pass


def _maint_base():
    """(root, key) maint tunnel se; (None, None) agar na mile."""
    try:
        live = os.path.join(
            os.path.expanduser("~"), "workspace",
            "goals/youtube-channel-management/hidden_files/maint_api",
            "live_url.txt")
        base = open(live, encoding="utf-8").read().strip()
        if "?key=" not in base:
            return None, None
        root, key = base.split("?key=", 1)
        return root.rstrip("/"), key
    except Exception:
        return None, None


def push_title_backfill(all_ids):
    """Queue/done/failed video IDs ke titles maint API ko bhejo (cache warm).

    Server ke title_cache mein jo IDs missing hain unke titles oEmbed se
    nikalo (yahan se — is VM ka IP block nahi) aur push-titles par bhejo.
    Har run mein max 15 naye (polite). Best-effort, kabhi fail nahi hota.
    """
    try:
        root, key = _maint_base()
        if not root or not all_ids:
            return
        with urllib.request.urlopen(
                "%s/api/maint/titles?key=%s" % (root, key),
                timeout=20) as r:
            cached = json.load(r).get("titles", {})
        missing = [v for v in all_ids if v and v not in cached][:15]
        if not missing:
            return
        titles = {}
        for vid in missing:
            t = oembed_title(vid)
            if t:
                titles[vid] = t
        if not titles:
            return
        data = json.dumps({"titles": titles}).encode("utf-8")
        req = urllib.request.Request(
            "%s/api/maint/push-titles?key=%s" % (root, key), data=data,
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log("TITLE-BACKFILL: %d titles pushed" % len(titles))
    except Exception as e:
        log("TITLE-BACKFILL skip: %s" % str(e)[:80])


def push_processing_info(running, queue_ids, done_ids, failed_ids, claims):
    """Abhi kaunsi video process ho rahi hai — maint API ko batao.

    claims mein video_id+title dono hain: jo claimed video na queue mein hai
    na done/failed mein, aur worker running hai -> wahi process ho rahi hai.
    Best-effort.
    """
    try:
        root, key = _maint_base()
        if not root:
            return
        payload = {"video_id": None, "title": ""}
        if running:
            busy = set(queue_ids) | set(done_ids) | set(failed_ids)
            cands = []
            for rec in (claims or {}).values():
                if not isinstance(rec, dict):
                    continue
                vid = rec.get("video_id")
                if vid and vid not in busy and rec.get("title"):
                    cands.append((rec.get("claimed_utc", ""), vid,
                                  rec.get("title")))
            if cands:
                cands.sort()
                _, vid, title = cands[0]
                payload = {"video_id": vid, "title": title}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "%s/api/maint/push-processing?key=%s" % (root, key), data=data,
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception:
        pass


def channel_videos(ch):
    """Latest videos: [(video_id, title, duration)]. URL ya channel_id dono chalte hain."""
    base = (ch.get("url") or "").rstrip("/")
    if not base:
        # 2026-09-28: portion-3 entries ke paas url hota hai; purani shape
        # ke liye channel_id fallback (KeyError ke bajaye wazeh error).
        _cid = ch.get("channel_id")
        if not _cid:
            raise RuntimeError("channel %s: na url na channel_id" %
                               ch.get("handle"))
        base = "https://www.youtube.com/channel/%s" % _cid
    out = subprocess.run(
        [YTDLP, "--no-warnings", "--flat-playlist",
         # 2026-09-27: VM ka egress proxy TLS MITM karta hai (apna CA chain mein);
         # is yt-dlp build mein --ca-certificate nahi aur SSL_CERT_FILE ignore hota
         # hai, is liye cert check skip. Sirf public channel-listing fetch hai.
         "--no-check-certificates",
         "--print", "%(id)s\t%(title)s\t%(duration)s",
         "--playlist-end", str(PLAYLIST_END),
         base + "/videos"],
        capture_output=True, text=True, timeout=CHANNEL_TIMEOUT)
    vids = []
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] and len(parts[0]) == 11:
            dur = parts[2] if len(parts) > 2 and parts[2] not in ("NA", "None", "") else None
            vids.append((parts[0], parts[1], dur))
    if not vids:
        raise RuntimeError("yt-dlp se koi video nahi mili: %s" % (out.stderr or "")[:200])
    return vids


def needs_review(combo, sig, drama):
    """Drama bilkul na nikle AUR koi sig bhi na bane -> True (review queue).

    combo ban gaya to solid hai (review nahi). Bina-number wala naya drama
    ("Yeni Dizi Tanitim": combo nahi, sig maujood) sig-claim par normal
    flow mein jata hai — review nahi. Sirf tab review jab key banane ko
    kuch bache hi na (masalan bare channel naam "TRT1").
    """
    if combo or sig:
        return False
    return True


def add_review(review, vid, title, channel, reason, now_iso):
    """promo_review.json mein entry (dedupe by video id, cap ke sath).
    Sirf storage — WhatsApp flow abhi nahi hai."""
    if any(r.get("id") == vid for r in review):
        return False
    review.append({"id": vid, "title": title, "channel": channel,
                   "reason": reason, "added_utc": now_iso})
    if len(review) > REVIEW_CAP:
        del review[:len(review) - REVIEW_CAP]
    log("REVIEW (%s): %s %s" % (reason, vid, title[:55]))
    return True


def quota_reached(uploads_done, running_now, cap=DAILY_CAP):
    """Aaj ke complete uploads + jo abhi process ho rahi hai >= cap."""
    return (uploads_done + running_now) >= cap


def may_submit(api_ok, queue_len, uploads_done, running_now,
               submitted_this_run, cap=DAILY_CAP):
    """Submit ki ijazat (user rule 2026-09-27: 1 process + 1 queue buffer,
    non-stop chain jab tak pending khaali na ho):
    - API theek ho, quota mein jagah ho, is run mein pehle submit na hua ho.
    - NOTE: server API ki queue mein PROCESS ho rahi video bhi shamil hai.
      running=1, queue=1 matlab sirf process ho rahi hai, buffer khaali -> submit OK
      running=1, queue=2 matlab 1 process + 1 buffer -> submit NAHI
      running=0, queue=0 matlab idle -> submit OK
      running=0, queue>=1 matlab start hone wali hai -> submit NAHI
    Process phir bhi STRICT sequential hai: agli video ka kaam tabhi shuru
    hota hai jab pichli ka upload MUKAMMAL ho jaye — queue sirf intezar hai."""
    if not api_ok or submitted_this_run:
        return False
    if quota_reached(uploads_done, running_now, cap):
        return False
    if running_now:
        return queue_len < 2  # 1 process ho rahi + max 1 buffer
    return queue_len == 0  # idle: queue bilkul khaali ho


def _claim_age_h(rec, now):
    try:
        ts = datetime.fromisoformat(
            rec.get("claimed_utc", "").replace("Z", "+00:00"))
        return (now - ts).total_seconds() / 3600.0
    except Exception:
        return 99.0


def stale_claim_vids(claims, done_ids, failed_ids, queue_ids, now,
                     min_age_h=2):
    """Wo claims jin ki video server par kahin nahi (na done, na failed,
    na queue) aur claim min_age_h se purana hai — submission gum ho gayi
    (server restart / queue wipe / worker crash). Wapas queue hone ke
    qabil. NOTE: sirf tab chalao jab API theek ho, warna har claim
    'missing' lagega."""
    out = []
    for key_, rec in claims.items():
        vid = rec.get("video_id")
        if not vid:
            continue
        if vid in done_ids or vid in failed_ids or vid in queue_ids:
            continue
        if _claim_age_h(rec, now) < min_age_h:
            continue  # abhi process ho rahi ho sakti hai
        out.append((key_, rec))
    return out


def classify_quarantine_item(it, done_map, claims, now):
    """Quarantine ke ek item ka faisla -> ('release'|'drop', wajah).

    QUARANTINE RULE (2026-09-26):
    - promo_pending_quarantine_*.json mein wo items hain jo quota-reset se
      pehle pending mein thay (purane / ghalat-parse wale). Inhe seedha
      pending mein dalna khatarnak tha, is liye quarantine kiya gaya tha.
    - Roz mein ek dafa har item NAYE gates se dobara parka jata hai:
      * added_utc se QUARANTINE_TTL_DAYS (30) se zyada purana
        -> 'drop', 'expired' (purana promo, ab koi faida nahi)
      * English title ya non-drama (game/daytime/reality)
        -> 'drop', 'english' / 'non-drama'
      * done/inflight ka duplicate -> 'drop', 'dup-done' / 'dup-inflight'
      * drama yaqeen se na nikle -> 'drop', 'ambiguous' (quarantine se
        review mein bhej kar atka rehne se behtar seedha drop hai)
      * baqi valid drama promo -> 'release' (pending mein wapas; wahan
        pacing + dup gates dobara lagenge)
    """
    vid = it.get("id")
    title = it.get("title", "")
    try:
        age_d = (now - datetime.fromisoformat(
            it.get("added_utc", "").replace("Z", "+00:00"))
                 ).total_seconds() / 86400.0
    except Exception:
        age_d = 0.0
    if age_d > QUARANTINE_TTL_DAYS:
        return "drop", "expired"
    # FIX 2026-10-03: English gate par koi exception nahi (sirf Turkish)
    if is_english_title(title):
        return "drop", "english"
    combo, sig, drama = parse_combo(title)
    if is_non_drama(title, drama):
        return "drop", "non-drama"
    if is_branded(title):
        return "drop", "branded"
    dup = find_dup(combo, sig, done_map, claims)
    if dup:
        return "drop", "dup-" + dup
    if (needs_review(combo, sig, drama)
            or _drama_from_affix_segment(_dequote(_norm(title)), drama)):
        return "drop", "ambiguous"
    return "release", ""


def process_quarantine(done_map, claims, now):
    """Tamam promo_pending_quarantine_*.json files process karo.
    Returns (released_entries, dropped_info)."""
    released, dropped = [], []
    for fp in sorted(glob.glob(os.path.join(
            HERE, "promo_pending_quarantine_*.json"))):
        items = load_json(fp, [])
        if not isinstance(items, list):
            continue
        for it in items:
            action, why = classify_quarantine_item(it, done_map, claims, now)
            vid = it.get("id")
            if action == "release":
                released.append({"id": vid, "title": it.get("title", ""),
                                 "channel": it.get("channel", ""),
                                 "combo": parse_combo(it.get("title", ""))[0],
                                 "sig": parse_combo(it.get("title", ""))[1],
                                 "added_utc": it.get("added_utc")})
            else:
                dropped.append({"id": vid, "why": why})
                log("QUARANTINE-DROP (%s): %s" % (why, (it.get("title") or "")[:55]))
        save_json(fp, [])  # sab ka faisla ho gaya — file khaali
    return released, dropped


def init_channel_baselines(state, channels, seen, now_iso):
    """Per-channel first-run baseline.

    Naye channel ki pehli scan mein us ki purani videos 'nayi' ban kar
    flood na karein — pehli scan sirf seen record karti hai.
    Migration: purane deployment mein 'seen' bhara hua hai -> maujooda
    tamam channels ko pehle se baselined mano. Bilkul naye setup mein
    (seen khaali) har channel ki pehli scan baseline hogi.
    Returns state['channel_baselines'] dict.
    """
    if "channel_baselines" not in state:
        if seen:
            state["channel_baselines"] = {
                c["handle"]: {"baseline_utc": now_iso(), "mode": "migrated"}
                for c in channels}
            log("baselines migrated: %d channels" % len(channels))
        else:
            state["channel_baselines"] = {}
    return state["channel_baselines"]


def _review_quarantine_cli():
    """FIX 7 (2026-10-01): Quarantine review path.

    Har promo_pending_quarantine_*.json file ke items list karo, har ek ke
    saath auto-classification (release/drop + wajah). Sirf review — koi
    item move/delete nahi hota.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    done_map = load_json(os.path.join(HERE, "promo_done.json"), {})
    claims = load_json(os.path.join(HERE, "promo_claims.json"), {})
    files = sorted(glob.glob(os.path.join(HERE, "promo_pending_quarantine_*.json")))
    if not files:
        print("Quarantine: koi file nahi mili.")
        return
    total = 0
    for fp in files:
        items = load_json(fp, [])
        if not items:
            continue
        print("== %s (%d items) ==" % (os.path.basename(fp), len(items)))
        for it in items:
            total += 1
            vid = it.get("id", "?")
            title = it.get("title", "")[:60]
            action, reason = classify_quarantine_item(it, done_map, claims, now)
            mark = "RELEASE" if action == "release" else "DROP"
            print("  [%s:%s] %s | %s" % (mark, reason, vid, title))
    print("Total quarantine items: %d" % total)


def _release_quarantine_item(vid):
    """FIX 7 (2026-10-01): Quarantine se ek item manually pending mein daalo."""
    if not vid:
        print("Usage: promo_watch.py --release-quarantine <video_id>")
        return
    import glob as _glob
    found = None
    found_fp = None
    for fp in sorted(_glob.glob(os.path.join(HERE, "promo_pending_quarantine_*.json"))):
        items = load_json(fp, [])
        for it in items:
            if it.get("id") == vid:
                found = it
                found_fp = fp
                break
        if found:
            break
    if not found:
        print("Quarantine mein %s nahi mila." % vid)
        return
    # FIX 7 HARDENING (2026-10-02): release se pehle tamam gates lagao —
    # bina gates ke release karna safety bypass tha. Ab ye checks lagte hain:
    # English-title, non-drama (title + drama naam), new-drama approval.
    _rtitle = found.get("title", "")
    _rcombo, _rsig, _rdrama = parse_combo(_rtitle)
    # 1. English-title gate (koi exception nahi — sirf Turkish)
    if is_english_title(_rtitle):
        print("BLOCKED (English title — sirf Turkish): %s" % _rtitle[:60])
        return
    # 2. Non-drama gate (title + drama naam dono par)
    if is_non_drama(_rtitle, _rdrama):
        print("BLOCKED (drama nahi — show/cooking): %s" % _rtitle[:60])
        return
    # 3. New-drama approval gate
    try:
        _alerts = load_json(os.path.join(HERE, "new_drama_alerts.json"), [])
        for _a in _alerts:
            if (_a.get("kind") == "new_drama"
                    and _a.get("drama_key") == _rdrama
                    and not _a.get("approved")
                    and not _a.get("skipped")):
                print("BLOCKED (naya drama — user approval ka intezar): %s" % _rdrama)
                return
    except Exception:
        pass
    # Tamam gates pass — ab release karo
    # Quarantine file se hatao
    items = load_json(found_fp, [])
    items = [it for it in items if it.get("id") != vid]
    save_json(found_fp, items)
    # Pending mein daalo
    pending = load_json(os.path.join(HERE, "promo_pending.json"), [])
    if isinstance(pending, dict):
        pending = pending.get("items", [])
    if not any(it.get("id") == vid for it in pending):
        pending.append(found)
        save_json(os.path.join(HERE, "promo_pending.json"), pending)
        print("RELEASED: %s pending mein daal diya." % vid)
    else:
        print("%s pehle se pending mein hai." % vid)


def main():
    # FIX 7 (2026-10-01): --review-quarantine: quarantine items ki review
    # list dikhao (har item + auto-classification), phir exit. Ye manual
    # review ka rasta hai — user/agent dekh sakta hai ke kya quarantine mein
    # hai aur kyun.
    if "--review-quarantine" in sys.argv:
        _review_quarantine_cli()
        return
    # FIX 7 (2026-10-01): --release-quarantine <video_id>: kisi quarantine
    # item ko manually pending mein daalo (review ke baad).
    if "--release-quarantine" in sys.argv:
        idx = sys.argv.index("--release-quarantine")
        vid = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        _release_quarantine_item(vid)
        return
    baseline = "--baseline" in sys.argv
    # drain-only: channel scan SKIP — sirf pending submit karna hai.
    # (user rule: upload mukammal hote hi sab se purani pending foran start ho)
    drain_only = "--drain-only" in sys.argv
    started = time.time()
    now_iso = lambda: datetime.now(timezone.utc).isoformat()

    channels = load_json(os.path.join(HERE, "watch_channels.json"), {}).get("channels", [])
    if not channels:
        raise RuntimeError("watch_channels.json khaali hai")
    # priority: main channels pehle (list order), phir drama-name channels
    channels = sorted(channels, key=lambda c: 0 if c.get("kind") == "main" else 1)
    # 2026-09-28: user-verified UNVERIFIED denylist — jin channels par blue
    # tick nahi (user ne khud dekha), wo kabhi scan nahi honge, chahe koi
    # unhe watch_channels.json mein dobara add kar de. (@DahaOnYediDizisi
    # se seekha: na tick tha, na exact naam — phir bhi scan ho raha tha.)
    denied = set(load_json(os.path.join(HERE, "unverified_channels.json"),
                           {}).get("handles", []))
    if denied:
        kept = []
        for c in channels:
            if c.get("handle") in denied:
                log("SKIP channel (user: unverified, no blue tick): %s"
                    % c.get("handle"))
                continue
            kept.append(c)
        channels = kept
    chan_by_handle = {c.get("handle"): c for c in channels}
    # 2026-09-28 PORTION 3 (user ka hukum): user ke EXPLICIT hukam par add
    # hue channels (user_channels.json). KOI GATE NAHI — na tick ka sawal,
    # na exact-naam ka, na 'kyun'. Portion 1+2 ki tarah har run mein scan
    # honge. Portion-3 membership unverified denylist ko OVERRIDE karti hai
    # (user ka seedha hukam > auto-denial).
    user_ch = load_json(os.path.join(HERE, "user_channels.json"), {}).get(
        "channels", [])
    seed_vids = {}
    for uc in user_ch:
        if not isinstance(uc, dict):
            continue
        h = (uc.get("handle") or "").strip()
        if not h:
            continue
        sv = [v for v in (uc.get("seed_videos") or []) if v]
        if sv:
            seed_vids.setdefault(h, set()).update(sv)
        if h in chan_by_handle:
            continue
        if h in denied:
            log("PORTION-3 override (user hukam): denylist ke bawajood "
                "scan hoga: %s" % h)
        entry = dict(uc)
        entry.setdefault("kind", "drama")
        channels.append(entry)
        chan_by_handle[h] = entry
    if user_ch:
        log("portion-3 channels: %d" % len(user_ch))
    # 2026-09-28 Daha-twice guard: known drama naam (drama resolve ke liye)
    _dl = load_json(os.path.join(HERE, "channel_drama_list.json"), {})
    _dramas = _dl.get("dramas", []) if isinstance(_dl, dict) else []
    # 2026-10-02: _fold_key (vowel-folded) taake resolve_drama keys
    # known_folded/display_map se match hon.
    known_dramas = sorted({_fold_key(d) for d in _dramas if d},
                          key=len, reverse=True)
    # 2026-09-29 new-drama detection: combo-drama (_norm folded) se compare
    # ke liye folded set + display naam ka map.
    known_folded = {_fold_key(d) for d in _dramas if d}
    display_map = {}
    for d in _dramas:
        if d:
            display_map.setdefault(_fold_key(d), d)
    # 2026-09-29 new-drama/new-season alert state (dedupe + notified flags)
    alerts_file = os.path.join(HERE, "new_drama_alerts.json")
    drama_alerts = load_json(alerts_file, [])
    if not isinstance(drama_alerts, list):
        drama_alerts = []
    seasons_file = os.path.join(HERE, "drama_seasons.json")
    drama_seasons = load_json(seasons_file, {})
    if not isinstance(drama_seasons, dict):
        drama_seasons = {}

    seen = load_json(os.path.join(HERE, "promo_seen.json"), {})
    sub = load_json(os.path.join(HERE, "promo_submitted.json"), {})
    state = load_json(os.path.join(HERE, "promo_watch_state.json"),
                      {"consec_failures": 0})
    claims = load_json(os.path.join(HERE, "promo_claims.json"), {})
    done_map = load_json(os.path.join(HERE, "promo_done.json"), {})
    pending = load_json(os.path.join(HERE, "promo_pending.json"), [])
    # 2026-09-27: kabhi kabhi file dict {"items": [...]} shape mein likhi gayi
    # (manual additions) — migrate_state ke liye hamesha list banao, warna
    # drain/watcher fatal crash karta hai aur pending submit ruk jati hai.
    if isinstance(pending, dict):
        log("pending: dict-wrapped shape milay, items list mein unwrap kiya")
        pending = pending.get("items", [])
    if not isinstance(pending, list):
        pending = []
    _raw_pending = pending
    pending = [p for p in _raw_pending if isinstance(p, dict)]
    if len(pending) != len(_raw_pending):
        log("pending: %d kharab entries chhodi gayin" %
            (len(_raw_pending) - len(pending)))
    review = load_json(os.path.join(HERE, "promo_review.json"), [])
    if not isinstance(review, list):
        review = []

    # per-channel first-run baselines (naye channels ki purani videos flood na karein)
    baselines = init_channel_baselines(state, channels, seen, now_iso)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if sub.get("date") != today:
        sub = {"date": today, "count": 0, "items": []}

    # purani keys ko naye canonical parse par lao + pending dups saaf karo
    claims, done_map, pending, _moved = migrate_state(
        claims, done_map, pending, now_iso)
    if _moved["claims"] or _moved["done"] or _moved["pending_dropped"]:
        log("migrate: claims_rekeyed=%d done_rekeyed=%d pending_dups_dropped=%d"
            % (_moved["claims"], _moved["done"], len(_moved["pending_dropped"])))

    summary = {"mode": "drain" if drain_only else ("baseline" if baseline else "watch"),
               "checked": 0, "new_videos": 0, "promo_candidates": 0,
               "queued": [], "dups": 0, "deferred_cap": [],
               "skipped_done": 0, "skipped_inflight": 0,
               "skipped_english": 0, "skipped_nondrama": 0,
               "pending": len(pending), "claims": len(claims),
               "queue_dup_of_done": [],
               "review_new": 0, "review": len(review),
               "reconciled": [], "quarantine": {"released": 0, "dropped": 0},
               "errors": [], "cap_hit": False}

    try:
        tunnel, key = ensure_dashboard()
    except Exception as e:
        raise RuntimeError("dashboard URL: %s" % e)

    def title_for(vid):
        s = seen.get(vid)
        if s and s.get("title"):
            return s["title"]
        return oembed_title(vid)

    def combo_of(vid, title):
        combo, sig, drama = parse_combo(title or "")
        return combo, sig, drama, claim_key(combo, sig)

    # ---- dashboard se done/failed/queue sync ----
    claimed_vids = {r.get("video_id"): k for k, r in claims.items()}
    status = api_status(tunnel, key)
    ch2 = (status.get("channels") or {}).get("ch2", {}) if status else {}
    done_ids = {vid_of(u) for u in ch2.get("done", [])} - {None}
    failed_ids = {vid_of(u) for u in ch2.get("failed", [])} - {None}
    queue_ids = {vid_of(u) for u in ch2.get("queue", [])} - {None}

    # ---- quota + pacing (2026-09-26 redesign, user-approved) ----
    # Quota sirf COMPLETED uploads par count hota hai (pending wali nahi).
    # Har run mein server ke done_ids ka diff dekhte hain — jo naye done hue,
    # wo aaj ke uploads hain. YouTube quota midnight PT par reset hota hai.
    # Pacing: aik waqt mein sirf 1 video queue mein jati hai (buffer). Agla
    # link tabhi submit hota hai jab server queue khaali ho — is se quota se
    # zyada videos kabhi start nahi hoti, 89% wali fail khatam.
    api_ok = bool(status) and "ch2" in (status.get("channels") or {})
    try:
        from zoneinfo import ZoneInfo
        today_pt = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        today_pt = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # new_done_ids: is run mein nazar aane wali nayi completed uploads
    # (last_done_ids se diff) — dono counters (PT + PKT) isi ko use karenge.
    # NOTE: ye PT-block se PEHLE compute hona chahiye, kyunki wo last_done_ids
    # ko update kar deta hai.
    new_done_ids = (done_ids - set(state.get("last_done_ids", []))) if api_ok else set()
    if state.get("quota_date") != today_pt:
        if "quota_date" in state:
            state["uploads_done"] = 0  # naya quota day
        else:
            # pehli dafa: aaj ka sahi count maloom nahi, is liye safe side
            state["uploads_done"] = DAILY_CAP
        state["quota_date"] = today_pt
        # Rollover par bhi last_done_ids sirf tab update karo jab API theek ho —
        # warna khaali done_ids snapshot ban jayega aur agle run mein purani
        # videos "nayi" gin li jayengi (2026-09-27 ko yahi bug uploads_done=10 kar gaya tha).
        if api_ok:
            state["last_done_ids"] = sorted(done_ids)
        log("quota day: %s (uploads_done=%d)" % (today_pt, state["uploads_done"]))
    else:
        # Sirf jab API theek ho tabhi diff/update karo — warna khaali done_ids
        # last_done_ids ko mita dega aur agle run mein purani videos "nayi"
        # gin li jayengi (uploads_done ghalat barh jayega).
        if api_ok:
            new_done = new_done_ids
            if new_done:
                state["uploads_done"] = state.get("uploads_done", 0) + len(new_done)
                log("upload complete: +%d (aaj kul %d)"
                    % (len(new_done), state["uploads_done"]))
            state["last_done_ids"] = sorted(done_ids)

    # ---- PKT day counter (user: "27 ki 12 AM se count") ----
    # YouTube ka asal quota PT-midnight (12 noon PKT) par reset hota hai —
    # oopar wala PT counter wahi track karta hai aur quotaExceeded fail se
    # bachata hai. Ye doosra counter Pakistan-day (midnight PKT) ke hisaab
    # se hai — user isi ko "aaj" samajhta hai. Submit sirf tab jab DONO mein
    # jagah ho; jo pehle full ho wahi rokega. Dono hi conservative hain.
    try:
        today_pkt = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%Y-%m-%d")
    except Exception:
        today_pkt = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("quota_date_pkt") != today_pkt:
        # Naya Pakistan-day: is run mein nazar aane wali nayi uploads isi
        # din ki gino (conservative — kuch thodi pehle ki bhi ho sakti hain,
        # lekin zyada ginna safe hai, kam ginna nahi).
        state["uploads_done_pkt"] = len(new_done_ids)
        state["quota_date_pkt"] = today_pkt
        log("PKT day: %s (uploads_done_pkt=%d)" % (today_pkt, state["uploads_done_pkt"]))
    elif new_done_ids:
        state["uploads_done_pkt"] = state.get("uploads_done_pkt", 0) + len(new_done_ids)

    running_now = 1 if ch2.get("running") else 0
    queue_len = len(ch2.get("queue", []))
    submitted_this_run = False

    def cap_reached():
        return (quota_reached(state.get("uploads_done", 0), running_now)
                or quota_reached(state.get("uploads_done_pkt", 0), running_now))

    def can_submit_now():
        return (may_submit(api_ok, queue_len, state.get("uploads_done", 0),
                           running_now, submitted_this_run)
                and may_submit(api_ok, queue_len, state.get("uploads_done_pkt", 0),
                               running_now, submitted_this_run))

    for key_, rec in list(claims.items()):
        vid = rec.get("video_id")
        if vid in done_ids:
            done_map[key_] = {"video_id": vid, "title": rec.get("title", ""),
                              "done_utc": now_iso()}
            del claims[key_]
            log("DONE: %s -> permanent skip (%s)" % (vid, rec.get("title", "")[:50]))
        elif vid in failed_ids:
            del claims[key_]
            log("FAILED: %s -> reservation khul gayi (%s)"
                % (vid, rec.get("title", "")[:50]))

    # queue mein jo videos hain (manual submit bhi) un ke combos claim karo
    claimed_vids = {r.get("video_id"): k for k, r in claims.items()}
    for qid in queue_ids:
        if qid in claimed_vids or qid in done_ids:
            continue
        title = title_for(qid)
        if not title or not is_promo(title, None):
            continue
        combo, sig, _drama, ck = combo_of(qid, title)
        # pehle se upload ho chuki promo dobara queue mein? -> warning
        # (server queue se hum nikal nahi sakte, user ko khabar karte hain)
        if find_dup(combo, sig, done_map, {}) == "done":
            summary["queue_dup_of_done"].append({"id": qid, "title": title[:70]})
            log("WARNING: queue wali ye promo pehle upload ho chuki lagti hai: "
                "%s %s" % (qid, title[:55]))
        if ck and ck not in done_map and ck not in claims:
            claims[ck] = {"video_id": qid, "title": title, "channel": "?",
                          "claimed_utc": now_iso(), "src": "queue"}
            log("CLAIMED (queue): %s %s" % (qid, title[:50]))

    # ---- submit retry reconcile (submission gum nahi hogi) ----
    # submit_link() ke baad server API par wo video kahin nahi (na done, na
    # failed, na queue) aur claim purana hai -> pending mein wapas.
    # Sirf jab API theek ho (warna har claim 'missing' lagega).
    pending_ids = {p.get("id") for p in pending}
    if api_ok:
        for key_, rec in stale_claim_vids(
                claims, done_ids, failed_ids, queue_ids,
                datetime.now(timezone.utc)):
            vid = rec.get("video_id")
            if vid in pending_ids:
                del claims[key_]
                continue
            title = rec.get("title", "")
            combo, sig, drama = parse_combo(title)
            if find_dup(combo, sig, done_map, claims):
                del claims[key_]
                log("RECONCILE: %s ab duplicate hai -> claim hataya" % vid)
                continue
            # FIX 2026-10-02: reconcile se wapas aane wali English video
            # pending mein nahi jayegi (drain/final-gate mein bhi pakri
            # jayegi, lekin yahin rokna saaf hai).
            if is_english_title(title):
                del claims[key_]
                log("RECONCILE-DROP (English): %s %s" % (vid, title[:50]))
                continue
            pending.append({"id": vid, "title": title,
                            "channel": rec.get("channel", ""),
                            "combo": combo, "sig": sig,
                            "added_utc": now_iso(), "from": "reconcile"})
            pending_ids.add(vid)
            del claims[key_]
            summary["reconciled"].append({"id": vid, "title": title[:70]})
            log("RECONCILE: submit gum thi, dobara pending mein: %s %s"
                % (vid, title[:55]))

    # ---- quarantine: din mein ek dafa classify karo ----
    if state.get("quarantine_date") != today:
        q_released, q_dropped = process_quarantine(
            done_map, claims, datetime.now(timezone.utc))
        for it in q_released:
            if it.get("id") in pending_ids:
                continue  # pehle se pending mein hai — dobara nahi
            dup = find_dup(it.get("combo"), it.get("sig"), done_map, claims)
            if dup:
                summary["quarantine"]["dropped"] += 1
                log("QUARANTINE-DROP (pending-dup %s): %s"
                    % (dup, it.get("id")))
                continue
            # FIX 2026-10-02: quarantine se release hone wali English video
            # bhi pending mein nahi jayegi.
            if is_english_title(it.get("title") or ""):
                summary["quarantine"]["dropped"] += 1
                log("QUARANTINE-DROP (English): %s %s"
                    % (it.get("id"), (it.get("title") or "")[:50]))
                continue
            pending.append(it)
            pending_ids.add(it.get("id"))
            summary["quarantine"]["released"] += 1
            log("QUARANTINE-RELEASE: %s %s" % (it.get("id"),
                                               (it.get("title") or "")[:55]))
        summary["quarantine"]["dropped"] += len(q_dropped)
        state["quarantine_date"] = today
        log("quarantine: released=%d dropped=%d"
            % (summary["quarantine"]["released"], len(q_dropped)))

    # pehli dafa: purane done videos ke combos seed karo
    if not state.get("done_seeded"):
        for did in done_ids:
            title = title_for(did)
            if not title:
                continue
            combo, sig, _drama, ck = combo_of(did, title)
            if ck and ck not in done_map:
                done_map[ck] = {"video_id": did, "title": title,
                                "done_utc": now_iso(), "src": "seed"}
        state["done_seeded"] = True
        log("done combos seeded: %d" % len(done_map))

    def do_submit(vid, title, handle, drama, combo, sig):
        # 2026-09-29 FINAL GATE (user order): queue mein jaane se aakhri lamha
        # pehle drama-naam check — cooking/non-drama video server queue mein
        # kabhi nahi jayegi, aur pending mein bhi wapas nahi aayegi.
        if is_non_drama(title, parse_combo(title or "")[2]):
            log("BLOCKED (queue-entry drama-gate — cooking/non-drama): "
                "%s %s" % (vid, (title or "")[:60]))
            summary["skipped_nondrama"] = \
                summary.get("skipped_nondrama", 0) + 1
            return "blocked-nondrama"
        # FIX 2026-10-02 (UJBrzAf4c88): FINAL GATE mein English check bhi —
        # koi bhi rasta (scan/drain/reconcile/quarantine/manual) se aaye,
        # English title yahan pakra jayega. Koi exception nahi (user
        # 2026-10-03: sirf Turkish — drama channel par bhi English nahi).
        if is_english_title(title or ""):
            log("BLOCKED (final gate — English title): %s %s"
                % (vid, (title or "")[:60]))
            summary["skipped_english"] = \
                summary.get("skipped_english", 0) + 1
            return "blocked-english"
        ck = claim_key(combo, sig)
        # FIX 2026-10-08: 3-channel fan-out (user order 2026-10-07) — har promo
        # ch2, phir ch3, phir ch4 par submit hota hai. Pehle sirf ch2 par jata tha.
        _fanout_msgs = {}
        for _ch in ("ch2", "ch3", "ch4"):
            _fanout_msgs[_ch] = submit_link(tunnel, key, vid, ch=_ch)
        # Sab se pehle unplayable check (koi bhi channel reject kare to quarantine)
        if "unplayable" in _fanout_msgs.values():
            msg = "unplayable"
        elif "error" in _fanout_msgs.values():
            msg = "error"
        elif all(m in ("ok", "dup") for m in _fanout_msgs.values()):
            msg = "ok"
        else:
            msg = "error"
        if msg == "unplayable":
            # NOTE: ye gate false-positive de sakta hai (oEmbed public hone
            # ke bawajood Error 153 — probe ki apni identity ka masla hota
            # hai). Is liye video ko quarantine mein MEHFOOZ karo, silent
            # drop kabhi nahi — review/retry ka rasta khula rahe.
            summary.setdefault("unplayable", []).append(
                {"id": vid, "title": title, "channel": handle})
            log("UNPLAYABLE (server playability gate reject — quarantine, "
                "retry nahi): %s %s" % (vid, (title or "")[:60]))
            return "unplayable"
        if msg == "error":
            # Known (2026-09-27): server busy ho to POST timeout karta hai
            # lekin video queue mein JA chuki hoti hai. Verify kar lo taake
            # ghalat "error" alarm na baje aur pending saaf ho jaye.
            try:
                _st = api_status(tunnel, key)
                _q = {_f for _f in
                      (vid_of(u) for u in _st.get("channels", {}).get(
                          "ch2", {}).get("queue", [])) if _f}
                if vid in _q:
                    msg = "ok"
                    log("submit timeout tha, video queue mein mil gayi -> ok: %s"
                        % vid)
            except Exception:
                pass
        if msg == "ok":
            sub["count"] += 1
            sub["items"].append(vid)
            if ck:
                claims[ck] = {"video_id": vid, "title": title,
                              "channel": handle, "claimed_utc": now_iso(),
                              "src": "watch"}
            summary["queued"].append(
                {"id": vid, "title": title, "channel": handle, "drama": drama})
            log("QUEUED: %s (%s) %s" % (vid, handle, title[:60]))
            # 2026-10-02 (user order: record rakhty jaen): jo drama queue mein
            # gaya us ka naam record mein pakka karo. Sirf tab jab drama
            # pehle se known ho (display_map) — naye/unknown naam auto-add
            # nahi hote, wo inform-only rehte hain.
            try:
                _dk = resolve_drama(
                    title, chan_by_handle.get(handle, {"kind": "main"}),
                    known_dramas)
                _disp = display_map.get(_dk)
                if _disp:
                    record_drama_name(_disp)
            except Exception:
                pass
        elif msg == "dup":
            summary["dups"] += 1
        else:
            summary["errors"].append("%s submit %s: %s" % (vid, handle, msg))
        return msg

    # ---- 1) pending queue pehle (aik waqt mein sirf 1 submit) ----
    # FIFO (user rule): sab se PURANI pending video pehle submit ho.
    try:
        pending.sort(key=lambda p: p.get("added_utc") or "9999")
    except Exception:
        pass
    still_pending = []
    for p in pending:
        if baseline:
            still_pending.append(p)
            continue
        # Safai HAR run mein (chahe submit ho ya na ho): done/inflight/
        # non-drama pending mein nahi rehne chahiye — warna ghalat ginti.
        # FIX 2026-10-02: video ID seedha server ke done list mein ho (bina
        # claim ke upload hui — koi bhi rasta) to pending se nikalo; combo-
        # match ke bharose mat raho (done_map mein key hi nahi hoti).
        if p.get("id") in done_ids:
            summary["skipped_done"] += 1
            log("PENDING skip (video ID server done mein — claim nahi thi): %s"
                % (p.get("title", "") or "")[:50])
            continue
        dup = find_dup(p.get("combo"), p.get("sig"), done_map, claims)
        if dup == "done":
            summary["skipped_done"] += 1
            log("PENDING skip (done): %s" % p.get("title", "")[:50])
            continue
        if dup == "inflight":
            summary["skipped_inflight"] += 1
            log("PENDING skip (inflight): %s" % p.get("title", "")[:50])
            continue
        # 2026-09-29 first-seen-wins: cross-title same promo ka final gate
        # (manual additions bhi yahan pakde jayenge).
        fw = fuzzy_first_wins(
            p.get("title", ""),
            chan_by_handle.get(p.get("channel"), {}),
            known_dramas, done_map, claims, pending,
            datetime.now(timezone.utc), exclude_id=p.get("id"))
        if fw:
            summary["skipped_first_wins"] = \
                summary.get("skipped_first_wins", 0) + 1
            log("PENDING skip (pehle wali copy pehle aa chuki — %s): %s"
                % (fw, p.get("title", "")[:50]))
            continue
        if is_non_drama(p.get("title", ""),
                        parse_combo(p.get("title", "") or "")[2]):
            summary["skipped_nondrama"] += 1
            log("PENDING skip (drama nahi — game/daytime/cooking show): %s" % p.get("title", "")[:50])
            continue
        # FIX 2026-10-02 (UJBrzAf4c88 duplicate): drain mein English-title
        # gate missing tha — "This Sea Will Overflow Episode 33 Trailer"
        # (@trt1) pending se submit ho gaya jabke Turkish copy pehle upload
        # ho chuki thi. Ab drain bhi scan jaisa gate lagayega.
        if is_english_title(p.get("title", "")):
            summary["skipped_english"] = summary.get("skipped_english", 0) + 1
            log("PENDING skip (English — sirf Turkish): %s" % p.get("title", "")[:50])
            continue
        if not can_submit_now():
            # quota full ho ya server queue mein buffer pehle se ho ->
            # pending mein hi rakho, agle run mein phir try hoga
            if cap_reached():
                summary["cap_hit"] = True
            still_pending.append(p)
            continue
        msg = do_submit(p["id"], p.get("title", ""), p.get("channel", ""),
                        p.get("channel", ""), p.get("combo"), p.get("sig"))
        # FIX 2026-09-27: aik run mein SIRF 1 submit attempt — natija jo bhi ho
        # (ok/dup/error/timeout). Pehle uncertain natijay par lock nahi lagta tha
        # aur isi run mein dosri video submit ho jati thi (2-at-a-time queue).
        submitted_this_run = True
        if msg == "dup":
            pass  # pehle se server par hai — pending se nikal gaya
        elif msg == "blocked-nondrama":
            pass  # ghalat video — pending se nikal gayi, retry kabhi nahi
        elif msg == "unplayable":
            # Server ke playability gate ne reject kiya — pending se nikal
            # kar promo_quarantine.json mein MEHFOOZ karo (review/retry ke
            # liye; gate false-positive ho sakta hai, isi liye silent drop
            # nahi). Pipeline block nahi hogi, video zaya bhi nahi hogi.
            try:
                _qf = os.path.join(HERE, "promo_quarantine.json")
                _q = json.load(open(_qf)) if os.path.exists(_qf) else []
                if not any(x.get("id") == p.get("id") for x in _q):
                    _q.append({
                        "id": p.get("id"), "title": p.get("title"),
                        "channel": p.get("channel"), "combo": p.get("combo"),
                        "sig": p.get("sig"), "added_utc": p.get("added_utc"),
                        "quarantined_utc": now_iso(),
                        "quarantine_reason": "server playability gate reject "
                            "(msg=unplayable) — gate false-positive ho sakta "
                            "hai, review/retry ke liye mehfooz",
                    })
                    json.dump(_q, open(_qf, "w"),
                              ensure_ascii=False, indent=1)
                    log("QUARANTINED (unplayable gate): %s" % p.get("id"))
            except Exception as _e:
                log("quarantine write fail: %s" % _e)
        elif msg != "ok":
            still_pending.append(p)  # agle run mein retry/reconcile
    pending = still_pending

    # ---- 2) channels scan (drain-only mein skip: naye promos hourly watcher dekhega) ----
    for ch in (channels if not drain_only else []):
        handle = ch["handle"]
        try:
            vids = channel_videos(ch)
        except Exception as e:
            summary["errors"].append("%s: %s" % (handle, str(e)[:120]))
            continue
        summary["checked"] += 1
        # per-channel first-run baseline: naye channel ki pehli scan sirf
        # seen record karti hai — us ki purani videos kabhi queue nahi hoti
        first_scan = handle not in baselines
        if first_scan:
            baselines[handle] = {"baseline_utc": now_iso(), "mode": "auto"}
            log("BASELINE: %s ki pehli scan — purani videos queue nahi hongi"
                % handle)
        for vid, title, dur in vids:
            if vid in seen:
                continue
            seen[vid] = {"title": title, "channel": handle,
                         "seen_utc": now_iso()}
            summary["new_videos"] += 1
            # 2026-09-28 portion 3: user ke diye hue seed links pehli scan
            # ke baseline ko bypass karte hain — baqi filters (is_promo,
            # Turkish-only, non-drama, dup) phir bhi lagte hain.
            _seeds = seed_vids.get(handle)
            if (baseline or first_scan) and not (_seeds and vid in _seeds):
                continue
            if not is_promo(title, dur):
                continue
            # FIX 2026-10-03: user ka blanket hukum (sirf Turkish, English
            # nahi) — drama ke exact-name channel par bhi English title
            # wali video SKIP hogi. Koi exception nahi.
            if is_english_title(title):
                summary["skipped_english"] += 1
                log("SKIP (English — sirf Turkish): %s (%s)"
                    % (title[:55], handle))
                continue
            if is_non_drama(title):
                summary["skipped_nondrama"] += 1
                log("SKIP (drama nahi — game/daytime show): %s (%s)"
                    % (title[:55], handle))
                continue
            summary["promo_candidates"] += 1
            combo, sig, drama, ck = combo_of(vid, title)
            # 2026-09-29 drama-gate (user order): drama KA NAAM check — cooking/
            # non-drama show kabhi pending mein nahi jayegi, chahe title mein
            # fragman kyun na ho ("Arda'nın Mutfağı" slip).
            if is_non_drama(title, drama):
                summary["skipped_nondrama"] += 1
                log("SKIP (drama naam cooking/non-drama: %s): %s (%s)"
                    % ((drama or "")[:40], title[:55], handle))
                continue
            if (needs_review(combo, sig, drama)
                    or _drama_from_affix_segment(
                        _dequote(_norm(title)), drama)):
                if add_review(review, vid, title, handle, "ambiguous-drama",
                              now_iso()):
                    summary["review_new"] += 1
                continue
            # 2026-09-29 new-drama / new-season detection (user order: sirf
            # INFORM — WhatsApp + main chat; list mein add user batayega).
            # Solid promo filters se guzar chuka hai, is liye signal pakka.
            # 2026-10-02 FIX: parse_combo ka drama quote-text se pollute ho
            # sakta hai ("haysiyetsadikkamyonabindive") jis se pehle se
            # record-shuda drama "naya" lagta hai. Is liye CLEAN resolve_drama
            # istemal karo (known-drama match pehle, parse fallback baad mein).
            _drama_key = resolve_drama(title, ch, known_dramas) or drama
            _new_now = detect_drama_events(title, ch, combo, sig, _drama_key,
                                           known_folded, display_map,
                                           drama_alerts, drama_seasons,
                                           now_iso())
            # 2026-10-01 portion-2 auto-eval (user order: "agar same naam hy
            # tu kr do"): naye drama ka exact-naam + tick channel mil jaye to
            # portion-2 watch mein add. Drama-list add ab bhi sirf inform.
            try:
                _dname = None
                for _a in _new_now:
                    if _a.get("kind") == "new_drama":
                        _dname = _a.get("name") or drama
                        break
                if _dname:
                    portion2_auto_eval(_dname, _new_now, now_iso())
            except Exception as _e:
                log("portion2 hook fail: %s" % str(_e)[:80])
            # FIX 8 (2026-10-01): New-drama approval gate (user order).
            # Naya drama (new_drama_alerts.json mein "new_drama" kind, bina
            # "approved": true) pending mein NAHI jayega — sirf inform hoga.
            # User khud check kar ke batayega. (Ömür Usta bina approval ke
            # pending mein chala gaya tha.)
            _needs_approval = False
            try:
                for _a in drama_alerts:
                    if (_a.get("kind") == "new_drama"
                            and _a.get("drama_key") == drama
                            and not _a.get("approved")
                            and not _a.get("skipped")):
                        _needs_approval = True
                        break
            except Exception:
                pass
            if _needs_approval:
                summary["skipped_new_drama_approval"] = \
                    summary.get("skipped_new_drama_approval", 0) + 1
                log("SKIP (naya drama — user approval ka intezar): %s (%s)"
                    % (title[:55], handle))
                continue
            dup = find_dup(combo, sig, done_map, claims)
            if dup == "done":
                summary["skipped_done"] += 1
                log("SKIP (upload ho chuka): %s (%s)" % (title[:55], handle))
                continue
            if dup == "inflight":
                summary["skipped_inflight"] += 1
                log("SKIP (pipeline mein hai): %s (%s)" % (title[:55], handle))
                continue
            # 2026-09-29 first-seen-wins: cross-title same promo
            # ("32. Bölüm 2. Fragmanı" vs "2. Sezon 2. Fragman") — pehle wali
            # copy jeetti hai, chahe wo main par ho ya drama channel par.
            fw = fuzzy_first_wins(title, ch, known_dramas, done_map,
                                  claims, pending,
                                  datetime.now(timezone.utc))
            if fw:
                summary["skipped_first_wins"] = \
                    summary.get("skipped_first_wins", 0) + 1
                log("SKIP (pehle wali copy pehle aa chuki — %s): %s (%s)"
                    % (fw, title[:55], handle))
                continue
            # 2026-09-28 Daha-twice guard: yehi promo pehle se pending mein?
            act, idx = pending_dup_action(pending, title, ch,
                                          known_dramas, chan_by_handle)
            if act == "skip":
                summary["skipped_pending_dup"] = \
                    summary.get("skipped_pending_dup", 0) + 1
                log("SKIP (pending mein same promo pehle se): %s (%s)"
                    % (title[:55], handle))
                continue
            if not can_submit_now():
                if cap_reached():
                    summary["cap_hit"] = True
                    summary["deferred_cap"].append(
                        {"id": vid, "title": title, "channel": handle})
                pending.append({"id": vid, "title": title, "channel": handle,
                                "combo": combo, "sig": sig,
                                "added_utc": now_iso()})
                continue
            msg = do_submit(vid, title, handle, ch.get("name", ""),
                            combo, sig)
            # FIX 2026-09-27: attempt hote hi lock — isi run mein dobara submit
            # nahi hoga, chahe natija uncertain/timeout ho.
            submitted_this_run = True
            if msg != "ok" and msg != "blocked-nondrama":
                # submit fail/dup (error/network) -> gum nahi, pending retry mein
                pending.append({"id": vid, "title": title, "channel": handle,
                                "combo": combo, "sig": sig,
                                "added_utc": now_iso(), "from": "submit-fail"})
                log("PENDING (submit fail, retry): %s %s"
                    % (vid, title[:55]))

    save_json(os.path.join(HERE, "promo_seen.json"), seen)
    save_json(os.path.join(HERE, "promo_submitted.json"), sub)
    save_json(os.path.join(HERE, "promo_claims.json"), claims)
    # POLICY 2026-10-01 (FIX 5): promo_done.json se kabhi auto-delete mat karo.
    # Agar YouTube se video delete ho jaye (jaise Taşacak recap 0vNxoeEk0cY),
    # to entry yahin rehne do — historical record hai. Duplicate detection
    # ke liye zaroori hai.
    save_json(os.path.join(HERE, "promo_done.json"), done_map)
    save_json(os.path.join(HERE, "promo_pending.json"), pending)
    save_json(os.path.join(HERE, "promo_review.json"), review)
    # 2026-09-29 new-drama/new-season alert state (sirf inform; list mein
    # add user ke hukam par hoga)
    save_json(alerts_file, drama_alerts)
    save_json(seasons_file, drama_seasons)
    # WhatsApp worker ke liye: abhi tak na-bataye gaye alerts summary mein
    summary["new_dramas"] = [
        a.get("name", "") for a in drama_alerts
        if a.get("kind") == "new_drama" and not a.get("notified_wa")]
    summary["new_seasons"] = [
        {"name": a.get("name", ""), "season": a.get("season")}
        for a in drama_alerts
        if a.get("kind") == "new_season" and not a.get("notified_wa")]

    # ---- pending display push (best-effort; run kabhi fail nahi hota) ----
    # Watcher ke baad server ka /pending page bhi update ho jaye taake user
    # ko pending list nazar aaye. Koi bhi error = silently skip.
    try:
        push_pending_display(pending)
    except Exception:
        pass
    # 2026-10-02: Android app ke liye pending.json GitHub par (app seedha
    # raw.githubusercontent se parhti hai). Best-effort.
    try:
        push_pending_github(pending)
    except Exception:
        pass

    # ---- manager app data: title backfill + processing info (best-effort) --
    # /app page ke liye: queue/done/failed ke drama names (title cache warm)
    # aur "abhi kya ban raha hai" card. Run kabhi fail nahi hota.
    try:
        if api_ok:
            push_title_backfill(set(queue_ids) | set(done_ids) | set(failed_ids))
            push_processing_info(bool(ch2.get("running")), queue_ids,
                                 done_ids, failed_ids, claims)
    except Exception:
        pass

    # drain-only: consec_failures ko NA chhuo — ye hourly watcher ke
    # channel-scan failure alerts ke liye hai; drain usay mask kar dega.
    if not drain_only:
        ok_run = not summary["errors"] or summary["checked"] > 0
        if ok_run and summary["checked"] > 0:
            state["consec_failures"] = 0
        else:
            state["consec_failures"] = state.get("consec_failures", 0) + 1
    state["last_run_utc"] = now_iso()
    save_json(os.path.join(HERE, "promo_watch_state.json"), state)
    summary["consec_failures"] = state["consec_failures"]
    summary["pending"] = len(pending)
    summary["claims"] = len(claims)
    summary["review"] = len(review)
    summary["elapsed_s"] = round(time.time() - started, 1)

    print("PROMO_WATCH_SUMMARY:" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("PROMO_WATCH_SUMMARY:" + json.dumps(
            {"mode": "fatal", "errors": [str(e)[:200]], "queued": []},
            ensure_ascii=False))
        sys.exit(1)

