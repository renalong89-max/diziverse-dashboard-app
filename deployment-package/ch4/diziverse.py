#!/usr/bin/env python3
"""
================================================================================
DiziVerse UNIVERSAL WORKFLOW -- one master file for EVERY video.
================================================================================

Single command, full automation (link -> uploaded video):

    python diziverse.py "https://www.youtube.com/watch?v=XXXX"

It runs, unattended, in this exact order:
    1.  Clean the previous video's artifacts (fresh state, nothing carries over)
    2.  Download the promo in 1080p
    3.  Cut a leading unrelated ad/promo if one is detected (1-4.5s hard cut)
    4.  Build the sub-18s talking hook montage (black fades, logo-hiding zoom)
    5.  Screenshot every scene (lossless PNG, zoomed, bad frames rejected)
    6.  Transcribe the trailer audio (Turkish, faster-whisper, on your PC)
    7.  Write the 7-8 minute suspenseful narration script (free keyless AI)
    8.  Generate the Turkish male voiceover (no captions anywhere)
    9.  Assemble final.mp4 (1920x1080, Zoom Lens + Bright Ember + Smoky Fog + Looming Fog)
    10. Generate the full Turkish SEO package from YOUR master prompt
        (raw title = promo title text before the first "|")
    11. Upload to YouTube (Entertainment, not made for kids)
    12. Build the suspenseful thumbnail from the best screenshot (+ plain text)
        and set it

HOW THIS FILE IS ORGANIZED (error prevention by design):
    PART 1 -- UNIVERSAL RULES (permanent). Every editing rule, effect value,
              SEO prompt, thumbnail rule and upload setting lives here as a
              named constant. These apply automatically to each new link and
              are NEVER changed per video.
    PART 2 -- PER-VIDEO STATE (VideoJob). Everything that differs per video
              (link, titles, SEO, file paths) lives in one object that is
              created fresh for every link. Nothing from one video can leak
              into another: artifacts are cleaned before each run and every
              reused file is verified against the current video.
    PART 3 -- shared utilities.
    PART 4 -- free keyless AI client (Pollinations, no signup, no key).
    PART 5 -- pipeline stages (each reads ONLY Part 1 + the current VideoJob).
    PART 6 -- main orchestrator.

OTHER COMMANDS:
    python diziverse.py --auth                  one-time Google sign-in
    python diziverse.py "URL" --skip-upload     build final.mp4, don't upload
    python diziverse.py --upload-only           upload the existing final.mp4
                                                (uses video_meta.json + SEO)
    python diziverse.py --upload-only --max-tags 5   tag-count diagnostic

The pinned comment is NOT posted automatically: the SEO package still
saves a pinned-comment text in seo_package.txt, but the user pastes it
manually later so the channel looks hand-managed.

REQUIREMENTS (all free):
    ffmpeg + ffprobe on PATH, yt-dlp on PATH
    pip install faster-whisper edge-tts opencv-python
    pip install google-api-python-client google-auth-oauthlib
    One-time: Google Cloud project with YouTube Data API v3 enabled +
              OAuth Desktop client saved as client_secrets.json here,
              then: python diziverse.py --auth
"""

import argparse
import asyncio
import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request


# =============================================================================
# PART 1 -- UNIVERSAL RULES (PERMANENT)
# -----------------------------------------------------------------------------
# Everything below is a permanent master rule. It applies to EVERY video link
# automatically. Do NOT edit these per video -- only the per-video data in
# PART 2 changes between runs.
# =============================================================================

# ---------------------------------------------------------------- 1.1 Hook montage
# The opening hook: talking clips, strictly UNDER 18 seconds total.
HOOK_MAX_TOTAL  = 18.0   # hard rule: final hook must stay UNDER 18 seconds
HOOK_MAX_PIECE  = 3.8    # hard rule: no single piece longer than this (< 4.0s)
HOOK_MIN_PIECE  = 2.5    # pieces shorter than this are skipped (not real talk)
HOOK_MAX_CLIPS  = 5      # never more than this many pieces
HOOK_GAP        = 5.5    # seconds left between pieces (5-6s, never sequential)
HOOK_FADE       = 0.3    # black fade transition length between pieces (seconds)
HOOK_ZOOM       = 1.25   # static zoom to hide corner logos/watermarks (25% in)

# ---------------------------------------------------------------- 1.2 Intro ad trim
# If the download opens with a short unrelated ad/promo (a hard scene cut
# 1.0-4.5s in), it is cut completely so the video starts with real content.
TRIM_LO = 1.0            # earliest cut position that counts as an intro ad
TRIM_HI = 4.5            # latest cut position that counts as an intro ad
TRIM_SCENE_THRESHOLD = 0.4  # ffmpeg scene sensitivity for the cut detection

# ---------------------------------------------------------------- 1.3 Screenshots
# Every scene of the trailer, lossless PNG, zoomed to hide corner logos and
# show details, framed 16:9. Bad frames are rejected automatically.
SCENE_ZOOM       = 1.25  # zoom factor: hides corner logos, shows details
SCENE_THRESHOLD  = 0.35  # scene detection sensitivity (lower = more shots)
SCENE_WIDTH      = 1280  # screenshot width  (PNG, lossless -- max quality)
SCENE_HEIGHT     = 720   # screenshot height (PNG, lossless -- max quality)
REJECT_BLACK     = 12    # drop frames with mean brightness below this
REJECT_WHITE     = 238   # drop frames with mean brightness above this
REJECT_FLAT_FRAC = 0.70  # drop frames with >70% pixels near the median
REJECT_FLAT_TOL  = 12    # ...within +/-12 of the median (logo/title cards)

# ---------------------------------------------------------------- 1.4 Transcription
# faster-whisper: free, open source, runs on your own PC, no API key.
WHISPER_MODEL = "base"   # tiny | base | small | medium (base: best speed/quality for clear trailer audio)
WHISPER_LANG  = "tr"      # trailer audio language

# ---------------------------------------------------------------- 1.5 Narration script
# Suspenseful third-person Turkish narration. The FINAL video must ALWAYS be
# 7-8 minutes: final = montage (<18s) + voiceover, so the voiceover targets
# ~7.25 min. The script's word target is computed from the MEASURED TTS rate
# on this PC (persisted in tts_wpm.txt; default 103 wpm measured from real
# runs -- the old 138 wpm figure was wrong, which is why videos ran 10.5 min).
# Short scripts are extended automatically (max 3 rounds). Scripts over the
# ceiling are trimmed to the last full sentence. If the target still cannot
# be reached, the run STOPS loudly -- a short video is never built silently.
TTS_WPM_FILE     = "tts_wpm.txt"
TTS_WPM_DEFAULT  = 103.0  # measured: ~1063-word script -> ~621s voiceover
VOICE_TARGET_S   = 435    # 7.25 min voiceover -> ~7.5 min final video
VOICE_MAX_S      = 460    # hard cap: voiceover never longer (final <= ~7.95 min)
VOICE_TRIM_S     = 445    # overshoot trim target (one re-synthesis, then done)
SCRIPT_MAX_TOKENS    = 2500
SCRIPT_EXTEND_TOKENS = 1500
SCRIPT_EXTEND_ROUNDS = 3
SCRIPT_AI_BUDGET     = 8 * 60  # max seconds for ALL free-AI calls in this
                               # stage; a degraded AI fails fast instead of
                               # burning 10+ minutes


def _tts_wpm():
    """Measured TTS words-per-minute on this PC (persisted across runs)."""
    try:
        with open(TTS_WPM_FILE, encoding="utf-8") as f:
            wpm = float(f.read().strip())
            if 40 < wpm < 250:
                return wpm
    except (OSError, ValueError):
        pass
    return TTS_WPM_DEFAULT


def _save_tts_wpm(wpm):
    try:
        with open(TTS_WPM_FILE, "w", encoding="utf-8") as f:
            f.write("%.1f\n" % wpm)
    except OSError:
        pass


def _script_targets():
    """(floor, ceil) word counts so the voiceover lands in the 7-8 minute
    band. Computed from the measured TTS rate: the floor aims at ~410s of
    voiceover (~7.1 min final), the ceiling at ~455s (~7.9 min final)."""
    wpm = _tts_wpm()
    floor = max(550, int(410 * wpm / 60.0))
    ceil = min(1050, int(455 * wpm / 60.0))
    if ceil - floor < 60:
        ceil = floor + 60
    return floor, ceil

# ---------------------------------------------------------------- 1.6 Voiceover
# edge-tts (Microsoft neural voices): free, no API key.
# Turkish MALE voice, natural/friendly. NO captions/subtitles anywhere.
VOICE_NAME   = "tr-TR-AhmetNeural"
VOICE_CHUNK  = 4000  # characters per TTS request

# ---------------------------------------------------------------- 1.7 Assembly & effects
# Final video: true 1920x1080, 30fps. Screenshot-only effects mapped from the
# exact CapCut values. No animated zoom on the hook montage (static logo-hiding
# crop only). No burnt-in captions, ever.
OUT_W, OUT_H, OUT_FPS = 1920, 1080, 30

ZOOM_SPEED  = 22    # ch4: increased from 18    # Zoom Lens Speed 18 (was 14): the 1.00 -> 1.23 sweep
                    # completes a bit faster, then holds the end zoom until
                    # the scene ends (faster-feeling zoom, same range)
ZOOM_RANGE  = 0.23  # Zoom Lens Range 23: eased sweep 1.00 -> 1.23
                    # (alternating push-in/pull-out)
ZOOM_SWEEP  = 14.0 / ZOOM_SPEED  # the sweep finishes this far into each
                    # scene (~0.78 at Speed 18); the rest is a hold

EMBER_SPEED = 28    # ch4: reduced from 33    # Bright Ember Speed 33: embers rise at ~33 px/s
EMBER_COUNT = 12    # ch4: increased from 7     # Bright Ember Atmosphere 7: a few subtle embers...
EMBER_ALPHA = 0.50  # ch4: reduced from 0.55  # ...at ~55% strength
EMBER_SEED  = 7     # fixed seed: identical ember layout on every video

FOG_ALPHA   = 0.30  # ch4: reduced from 0.40   # Smoky Fog Atmosphere 40 (user 2026-09-21: back to STRONG
                    # from medium; fx23 sample ka level 3, bottom-third lift
                    # ~21/255 luma — survives YouTube compression)
                    # (Smoky Fog Speed 4 = ultra-natural 3-harmonic drift:
                    #  one slow 45s sweep + gentle gusts, seamlessly looped)
LOOM_SPEED  = 4     # Looming Fog Speed 4: slow cloud-like drift on a 45s loop
                    # (the slowest set)
LOOM_ALPHA  = 0.10  # Looming Fog Atmosphere 10: a 10% opacity veil, bottom-weighted
                    # with a soft absorbed gradient -- natural cloud behaviour
LOOM_PAD_X  = 280   # Loom layer is rendered this many px wider than the frame
LOOM_PAD_Y  = 100   # ...and this many px taller, so the Speed-4 drift can never
                    # uncover a bare edge (no visible seam while it moves)
FOG_PAD_X   = 140   # Smoky-fog layer is rendered this many px wider than the frame
FOG_PAD_Y   = 80    # ...and this many px taller, so its drift can never uncover
                    # a bare edge (fixes the moving right/bottom strip)
ATMOS_LOOP  = 45    # seconds; the pre-rendered fog/ember loop length

# Video encoder: Intel Quick Sync hardware (h264_qsv) when the machine really
# supports it (verified with a 1s test encode at startup), else libx264.
ENC_QUALITY = 20    # crf / global_quality (same visual quality either way)

# ---------------------------------------------------------------- 1.8 YouTube upload
YOUTUBE_CATEGORY_ID  = "24"     # Entertainment
YOUTUBE_MADE_FOR_KIDS = False   # audience: NOT made for kids
YOUTUBE_PRIVACY      = "public"
# Resumable-upload retries: a single dropped chunk must never kill a run.
# Google's own API samples retry next_chunk() with exponential backoff;
# the request object resumes from the server's confirmed byte offset.
UPLOAD_MAX_RETRIES   = 10
YOUTUBE_TAG_BYTES    = 495      # tag field budget (API counts UTF-8 bytes)
# Tag-count diagnostic: YouTube documents 500 chars, but a 400/invalidTags
# rejection was observed on a 451-char/470-byte tag set that satisfied every
# documented rule. Keep None (= send all tags, byte-trimmed) until the real
# limit is bisected with --max-tags; then set this number permanently.
YOUTUBE_MAX_TAGS = None

# ---------------------------------------------------------------- 1.9 Thumbnail
# Thumbnail rule: build it from the video's OWN screenshots, not the promo
# thumbnail. Pick the most suspenseful shot (biggest face close-up, else
# highest contrast/sharpness). Check BOTH top corners independently and
# remove ONLY actual source/channel branding there via inpainting -- a logo
# never appears on the new thumbnail. NEVER remove larger drama/title text,
# even in a corner. Draw ONE short line of PLAIN text on the image (bold,
# solid color -- no designed/styled text effects, no symbols/icons/emoji).
# Everything else (faces, background, colors, composition) stays unchanged.
THUMB_CORNER_W   = 0.20  # corner inspection zone: 20% of width
THUMB_CORNER_H   = 0.25  # corner inspection zone: 25% of height (logos often
                         # extend a little below the very top edge)
LOGO_MAX_W       = 0.12  # logos are SMALL: wider than 12% = title text, keep
LOGO_MAX_H       = 0.16  # taller than 16% = title text, keep
LOGO_MAX_AREA    = 0.02  # bigger than 2% of area = title text, keep
LOGO_MIN_DENSITY = 0.08  # blob must look like a graphic, not noise
LOGO_CLUSTER_GAP = 18   # nearby graphic pieces within 18px merge into one
                        # logo candidate; distant texture never joins
TEXT_BANNER_MAX  = 0.05 # (legacy) episode/fragman banner filter -- no longer
                        # used for exclusion: the user's samples KEEP the
                        # "N. BOLUM / N. FRAGMAN / TANITIM" bottom text.
THUMB_TITLE_MIN  = 0.05 # drama-name preference: a screenshot whose top
                        # third holds big title text (score above this) is
                        # preferred for the thumbnail -- the user wants the
                        # drama name at top-left/right/middle as in the
                        # samples. If no screenshot has one, the best
                        # people shot wins anyway.
BOTTOM_TEXT_MIN  = 0.02 # bottom-text preference: a screenshot whose bottom
                        # third holds bolum/fragman text (score above this)
                        # is preferred -- the user's samples always keep
                        # the "N. BOLUM / N. FRAGMAN" line at the bottom.
THUMB_TITLE_MIN  = 0.05 # drama-title preference: a screenshot whose top
                        # third holds big title text (score above this) is
                        # preferred for the thumbnail -- the user wants the
                        # drama name at top-left/right as in the samples.
                        # If no screenshot has one, the best people shot
                        # wins anyway.

# ---------------------------------------------------------------- 1.10 SEO rules (permanent procedure)
# For EVERY video, without exception:
#   1. Read the promo title.
#   2. Take only the text before the first "|".
#   3. The script BUILDS the complete SEO package itself from that title
#      (drama name, episode number, fragman number, quoted hook) following
#      the master SEO prompt's structure below -- no AI service needed, so
#      this step is instant and can never get stuck or fail.
#   4. The universal file seo_package.txt (tagged "# VIDEO: <raw title>")
#      carries the data; every upload reads from it.
#   5. Never reuse one video's SEO for another video (the tag guard
#      enforces this). Category: Entertainment. Not made for kids.
# The prompt below is the structure spec the builder implements -- it is
# NEVER edited per video; only the raw title changes.

# ---------------------------------------------------------------- 1.11 The master SEO prompt (VERBATIM -- do not edit)
SEO_PROMPT = """**You are a YouTube SEO expert specializing in Turkish drama channels with a worldwide audience (Turkey, Pakistan, India, Arabia, Europe, USA).**

Your task is to transform my raw Turkish drama title into a complete YouTube SEO package with the highest possible CTR and search optimization.

I will only provide the RAW TITLE.

You must generate everything in fluent Turkish.

━━━━━━━━━━━━━━━━━━━━━━

### 1️⃣ LONG VIDEO PACKAGE

✅ Give me 5 HIGH CTR rewritten titles (Maximum 60 characters)

Rules:
• Start with the Drama Name
• Add Episode Number
• Add Emotional Hook
• Add Year (2026) only if it feels natural
• Make every title curiosity-driven
• Avoid clickbait that contradicts the title I provide.

After that,
Choose the BEST TITLE and explain in one sentence why it is the strongest.

━━━━━━━━━━━━━━━━━━━━━━

### 2️⃣ SEO DESCRIPTION (LONG VIDEO)

Write an SEO-optimized description in TWO PARAGRAPHS.

Paragraph 1:
Explain the trailer naturally using the drama name, episode number and emotional hook.

Paragraph 2:
Invite viewers to subscribe to DiziVerse, like, comment and enable notifications in a natural way.

Do NOT stuff keywords.
Write naturally for Turkish viewers.

━━━━━━━━━━━━━━━━━━━━━━

### 3️⃣ KEYWORDS

Generate at most 27 high-search YouTube keywords separated by commas.

Include:
• Drama Name
• Episode Number
• Fragman
• Yeni Bölüm
• Turkish Drama related keywords
• 2026 when relevant

━━━━━━━━━━━━━━━━━━━━━━

### 4️⃣ HASHTAGS

Generate exactly 10 hashtags.

Include:
• Drama name
• Fragman
• YeniBölüm
• TürkDizileri
• DiziVerse

━━━━━━━━━━━━━━━━━━━━━━

### 5️⃣ PINNED COMMENT

Write one engaging Turkish pinned comment that encourages discussion and subscriptions.

━━━━━━━━━━━━━━━━━━━━━━

### 6️⃣ THUMBNAIL TEXT

Generate 3 very short thumbnail texts (2–5 words each) with high emotional impact.

━━━━━━━━━━━━━━━━━━━━━━

### 7️⃣ SHORTS VIDEO PACKAGE

Generate a separate package for YouTube Shorts.

Include:

✅ 3 Viral Shorts Titles (Maximum 45 characters)

✅ Shorts Description

Write the Shorts description in TWO PARAGRAPHS.

Paragraph 1:
Create curiosity about the scene.

Paragraph 2:
Invite viewers to subscribe to DiziVerse.

✅ Shorts Keywords

Generate 20 SEO keywords.

✅ Shorts Hashtags

Generate exactly 8 hashtags.

━━━━━━━━━━━━━━━━━━━━━━

### 8️⃣ SEO SCORE

Rate everything:

CTR Score /10

SEO Score /10

Viral Potential /10

Explain briefly why.

━━━━━━━━━━━━━━━━━━━━━━

Output should be clean, professional and easy to copy.

Never write anything in English except universally recognized terms like CTR or SEO.

RAW TITLE:

{raw_title}

---
AUTOMATION NOTE (do this after the package above): repeat these 5 fields inside
the exact markers below so software can read them. Content stays in Turkish.
[SEO_TITLE]
(the single BEST TITLE, max 60 characters)
[/SEO_TITLE]
[SEO_DESCRIPTION]
(the 2-paragraph SEO description, paragraphs separated by a blank line)
[/SEO_DESCRIPTION]
[SEO_KEYWORDS]
(at most 27 keywords, comma separated, no # symbols)
[/SEO_KEYWORDS]
[SEO_HASHTAGS]
(the exactly 10 hashtags, space separated, with # symbols)
[/SEO_HASHTAGS]
[SEO_PINNED]
(the Turkish pinned comment)
[/SEO_PINNED]
"""

# ---------------------------------------------------------------- 1.12 File layout (permanent names)
# Per-video artifacts (deleted by the cleaner before each new video):
F_SOURCE_GLOB   = "source_*.mp4"
F_MONTAGE       = "montage.mp4"
F_FINAL         = "final.mp4"
F_SCENES_DIR    = "scenes"
F_TRANSCRIPT    = "transcript.txt"
F_SCRIPT        = "script.txt"
F_VOICE         = "voice.mp3"
F_META          = "video_meta.json"
F_THUMB_RAW     = "thumb_raw"
F_THUMB_CLEAN   = "thumbnail_clean.jpg"
F_BUILD_DIR     = "_build"
# FX_CACHE_DIR holds content-independent effect assets (fog layer, ember
# sprite/loop, merged atmosphere loop). They are identical for every video,
# so they are built ONCE and never deleted by the cleaner.
FX_CACHE_DIR    = "fx_cache"
# F_SEO (seo_package.txt) is UNIVERSAL and PERMANENT -- the cleaner never
# deletes it. Every upload reads the SEO data from this one file. The
# "# VIDEO: <raw title>" tag inside still guards isolation: if the file
# belongs to a different video, fresh SEO is generated instead of reusing it.
F_SEO           = "seo_package.txt"
# Permanent files (NEVER deleted): this script, client_secrets.json,
# token.json, seo_package.txt, and the fx_cache/ folder.
CLEAN_PATTERNS = ["source_*.mp4", "montage.mp4", "final.mp4", "transcript.txt",
                  "script.txt", "voice.mp3", "voice.srt", "video_meta.json",
                  "thumb_raw.*", "thumbnail_clean.jpg"]
CLEAN_DIRS = ["scenes", "_build", "__pycache__", "build"]

YOUTUBE_SCOPES   = ["https://www.googleapis.com/auth/youtube.upload",
                    "https://www.googleapis.com/auth/youtube.force-ssl"]
CLIENT_SECRETS = "client_secrets.json"
TOKEN_FILE     = "token.json"


# =============================================================================
# PART 2 -- PER-VIDEO STATE (VideoJob)
# -----------------------------------------------------------------------------
# Everything that differs between videos lives in ONE object, created fresh
# for every link. Stages may ONLY read per-video data from the current job --
# never from leftover files. Before each run the cleaner wipes the previous
# video's artifacts, and every reused file is verified against the job.
# =============================================================================
class VideoJob:
    """All data belonging to ONE video link. Fresh instance per run."""

    def __init__(self, url):
        self.url = url.strip()
        # --- filled by stage_download ---
        self.video_id = ""       # YouTube id extracted from the URL
        self.source_file = ""    # local source_*.mp4 (ad-trimmed)
        self.full_title = ""     # promo title from YouTube
        self.raw_title = ""      # text before the first "|"
        # --- filled by stage_seo ---
        self.seo_title = ""
        self.seo_description = ""
        self.seo_tags = []
        self.seo_hashtags = []
        self.seo_pinned = ""
        # --- filled by stage_upload ---
        self.youtube_video_id = ""
        self.youtube_watch_url = ""

    def check_meta(self):
        """Verify video_meta.json on disk belongs to THIS job's URL.

        This is the isolation guard: if a stale meta file from a previous
        video is somehow present, the run stops instead of mixing data."""
        if not os.path.isfile(F_META):
            return
        try:
            with open(F_META, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            fail("Isolation", "%s is unreadable -- delete it and run again."
                 % F_META)
        if meta.get("url") and meta.get("url") != self.url:
            fail("Isolation",
                 "%s belongs to a different video!\n"
                 "  file URL: %s\n"
                 "  this run: %s\n"
                 "Delete the stale files and run again." %
                 (F_META, meta.get("url"), self.url))


# =============================================================================
# PART 3 -- shared utilities
# =============================================================================
def fail(stage, message):
    """Stop the whole run with a clear, actionable error."""
    sys.exit("\nFAILED at '%s':\n%s\n" % (stage, message))


def banner(stage, total, text):
    print("\n" + "=" * 60)
    print("[%d/%d] %s  (%s)" % (stage, total, text, time.strftime("%H:%M:%S")))
    print("=" * 60)


def run_ffmpeg(cmd, what="ffmpeg"):
    # stdin=DEVNULL: ffmpeg's interactive keys (e.g. 'c' -> "Enter command:")
    # must never hijack the console and pause a run on a stray keypress.
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        fail(what, "ffmpeg command failed: %s" % " ".join(cmd[:6]))


def ffprobe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        fail("probe", "Could not read duration of %s" % path)


def ffprobe_has_audio(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return bool(out.stdout.strip())


def need(path, stage):
    if not os.path.isfile(path):
        fail(stage, "Expected file was not created: %s" % path)


def extract_video_id(url):
    """Pull the YouTube video id out of a watch/shorts/youtu.be URL."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{6,})", url)
    return m.group(1) if m else ""


# =============================================================================
# PART 4 -- free keyless AI (Pollinations.ai -- no signup, no key, no payment)
# -----------------------------------------------------------------------------
# The ONLY text-AI provider in this workflow. Strategy, all automatic:
#   1. POST to the OpenAI-compatible endpoint (handles long prompts) --
#      2 long attempts (up to 10 minutes each); big generations legitimately
#      take several minutes, so short timeouts are never used.
#   2. Fall back to the classic GET endpoint -- 2 tries.
# =============================================================================
_AI_POST_URL = "https://text.pollinations.ai/openai"
_AI_GET_URL = "https://text.pollinations.ai/"


def _ai_post(prompt, max_tokens, temperature, timeout=180, attempts=2):
    body = json.dumps({
        "model": "openai",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    print("Waiting for free AI (big text can take several minutes) ...")
    delay = 15
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(
                _AI_POST_URL, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip()
            if text:
                return text
            raise ValueError("empty response")
        except Exception as e:
            if attempt < attempts:
                wait = delay + random.uniform(0, 5)
                print("Free AI hiccup (try %d/%d: %s), waiting ~%ds ..."
                      % (attempt, attempts, type(e).__name__, int(wait)))
                time.sleep(wait)
                delay = min(delay * 2, 60)
    return None


def _ai_get(prompt, attempts=2):
    delay = 10
    for attempt in range(1, attempts + 1):
        try:
            url = _AI_GET_URL + urllib.parse.quote(prompt, safe="")
            with urllib.request.urlopen(url, timeout=120) as r:
                text = r.read().decode("utf-8").strip()
            if text:
                return text
            raise ValueError("empty response")
        except Exception as e:
            if attempt < attempts:
                wait = delay + random.uniform(0, 5)
                print("Free AI fallback hiccup (try %d/%d: %s), waiting ~%ds ..."
                      % (attempt, attempts, type(e).__name__, int(wait)))
                time.sleep(wait)
                delay = min(delay * 2, 60)
    return None


def free_generate(prompt, max_tokens=2500, temperature=0.8):
    """Generate text with the free keyless provider. Returns text or None."""
    print("Generating with free AI (Pollinations, no key) ...")
    text = _ai_post(prompt, max_tokens, temperature)
    if text is None:
        print("Trying simple endpoint ...")
        text = _ai_get(prompt)
    return text


# =============================================================================
# PART 5 -- PIPELINE STAGES
# -----------------------------------------------------------------------------
# Each stage reads ONLY the universal rules (PART 1) and the current
# VideoJob (PART 2). No stage keeps global per-video state.
# =============================================================================

# ---------------------------------------------------------------- Stage 1: clean
def stage_clean(job):
    """Wipe the previous video's artifacts so the new video starts fresh.

    Keeps: this script, client_secrets.json, token.json, seo_package.txt
    (the universal SEO file -- every upload reads its data from there).
    With job.keep_script, script.txt is also kept (your hand-written script).
    Deletes: every per-video artifact listed in PART 1.12."""
    removed = []
    for pat in CLEAN_PATTERNS:
        for f in glob.glob(pat):
            if os.path.isfile(f):
                if f == F_SCRIPT and getattr(job, "keep_script", False):
                    continue
                os.remove(f)
                removed.append(f)
    for d in CLEAN_DIRS:
        if os.path.isdir(d):
            shutil.rmtree(d)
            removed.append(d + "/")
    if removed:
        print("Deleted %d item(s) from the previous video." % len(removed))
    else:
        print("Folder is already fresh.")
    print("Kept: diziverse.py, client_secrets.json, token.json, seo_package.txt, fx_cache/")


# ---------------------------------------------------------------- Stage 2: download
def _first_cut_after(src):
    """pts_time of the first hard scene cut inside [TRIM_LO, TRIM_HI], or None.

    A hard cut 1-4.5s in means the video opens with a short unrelated
    ad/promo before the real drama content starts."""
    cmd = ["ffmpeg", "-v", "info", "-i", src,
           "-filter:v", "select='gt(scene,%s)',showinfo" % TRIM_SCENE_THRESHOLD,
           "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    for line in r.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
            if t > TRIM_HI:
                break
            if t >= TRIM_LO:
                return t
    return None


def _strip_leading_ad(src):
    """Cut a leading unrelated ad/promo; leave the file untouched otherwise."""
    t = _first_cut_after(src)
    if t is None:
        print("Intro check: starts with real content, keeping from 0s.")
        return src
    print("Intro check: removing %.1fs leading ad/promo ..." % t)
    tmp = src + ".trimmed.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-ss", "%.3f" % t, "-i", src,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", tmp])
    if r.returncode != 0 or not os.path.isfile(tmp):
        print("WARNING: ad trim failed, keeping original file.")
        return src
    os.replace(tmp, src)
    print("Intro trimmed -> %s starts at the real content." % src)
    return src


def _write_video_meta(job):
    meta = {"url": job.url,
            "file": os.path.abspath(job.source_file),
            "video_id": job.video_id,
            "title": job.full_title}
    with open(F_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)


def _cookie_files():
    """Cookie files jo maujood hain, tarteeb ke sath.

    cookies3.txt (server ke liye alag dedicated Google account) sab se
    pehle try hota hai taake main account par do jagah se istemal ka
    shak na ho. cookies.txt / cookies2.txt sirf backup hain — agar
    dedicated wali expire ho jaye to pipeline khud agli file try karta
    hai, user ko foran naye cookies bhejne ki zaroorat nahi parti.
    """
    return [f for f in ("cookies3.txt", "cookies.txt", "cookies2.txt")
            if os.path.isfile(f)]


def _probe_cookies(url):
    """Pehli woh cookie file jo YouTube ke bot-check se guzar jaye.

    Returns the working file name, or None agar koi bhi kaam na kare.
    """
    for cf in _cookie_files():
        try:
            r = subprocess.run(
                _ytdlp_cmd("--skip-download", "--print", "%(id)s", url,
                           cookie_file=cf),
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                timeout=120)
            blob = (r.stdout or "") + (r.stderr or "")
            bad = ("no longer valid" in blob or "not a bot" in blob
                   or "confirm you" in blob)
            if r.returncode == 0 and not bad:
                return cf
            print("  cookie check %s: expired/blocked, agli try kar rahe hain" % cf)
        except Exception as e:
            print("  cookie check %s: probe error %s" % (cf, e))
    return None


def _ytdlp_cmd(*args, cookie_file=None):
    """yt-dlp command with automatic cookies.txt support.

    YouTube kabhi-kabhi server ke IP ko block kar deta hai
    ('Sign in to confirm you are not a bot'). Is surat mein apne browser
    se YouTube ke cookies export karke 'cookies.txt' ke naam se is folder
    (channel wale folder) mein rakhein — tamam yt-dlp calls khud hi
    use karengi. Cookies purane hon to dobara export karke replace karein.

    Zyada behtari ke liye cookies2.txt / cookies3.txt (kisi aur Google
    account se) bhi rakhe ja sakte hain — _probe_cookies() khud pehli
    kaam karne wali file chun lega.
    """
    cmd = ["yt-dlp", "--no-playlist", "--remote-components", "ejs:github"]
    cf = cookie_file or "cookies.txt"
    if os.path.isfile(cf):
        cmd += ["--cookies", cf]
    cmd.extend(args)
    return cmd


_FFMPEG_MAJOR = None
def _ffmpeg_major():
    global _FFMPEG_MAJOR
    if _FFMPEG_MAJOR is None:
        try:
            out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"version (\d+)\.", out)
            _FFMPEG_MAJOR = int(m.group(1)) if m else 5
        except Exception:
            _FFMPEG_MAJOR = 5
    return _FFMPEG_MAJOR
def _vfr_flag():
    return ["-fps_mode", "vfr"] if _ffmpeg_major() >= 5 else ["-vsync", "vfr"]

def stage_download(job, keep_intro=False):
    """Download the promo in 1080p, trim a leading ad if present, save the
    promo title -> raw title (text before the first "|")."""
    if not shutil.which("yt-dlp"):
        fail("download", "yt-dlp not found. Install it first: winget install yt-dlp.yt-dlp")
    if not shutil.which("ffmpeg"):
        fail("download", "ffmpeg not found. Install it first: https://ffmpeg.org/download.html")

    job.video_id = extract_video_id(job.url)
    out = os.path.join(".", "source_%(id)s.%(ext)s")

    # Kaam karne wali cookie file chunein (cookies.txt -> cookies2.txt -> cookies3.txt).
    # Agar koi bhi file bot-check se na guzre to foran saaf message dein.
    cfiles = _cookie_files()
    job.cookie_file = _probe_cookies(job.url) if cfiles else None
    if cfiles and not job.cookie_file:
        fail("download", "yt-dlp download failed (tamam cookies purani ho gayi hain — cookies.txt / cookies2.txt / cookies3.txt mein se koi kaam nahi kar rahi). Browser se naye YouTube cookies export karke update karein.")
    if job.cookie_file:
        print("Cookies OK: %s" % job.cookie_file)

    cmd = _ytdlp_cmd(
           "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
           "--merge-output-format", "mp4",
           "-o", out, job.url, cookie_file=job.cookie_file)
    print("Downloading 1080p ...")
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        if _cookie_files():
            fail("download", "yt-dlp download failed (download ke doran cookies expire ho gayi — browser se naye YouTube cookies export karke cookies.txt update karein).")
        fail("download", "YouTube ne server ka IP block kiya hai ('Sign in to confirm you are not a bot'). Hal: browser mein YouTube kholein, cookies export karke 'cookies.txt' ke naam se channel folder mein rakhein.")
    cands = [f for f in os.listdir(".")
             if f.startswith("source_") and f.endswith(".mp4")]
    if not cands:
        fail("download", "Download finished but no source_*.mp4 found.")
    job.source_file = os.path.join(".", sorted(cands)[-1])
    print("Saved -> %s" % job.source_file)

    if not keep_intro:
        job.source_file = _strip_leading_ad(job.source_file)

    # promo title for the upload stage (title -> raw title -> SEO)
    try:
        r = subprocess.run(_ytdlp_cmd("--skip-download",
                            "--print", "%(title)s", job.url,
                            cookie_file=job.cookie_file),
                           capture_output=True, text=True)
        job.full_title = r.stdout.strip()
    except Exception:
        job.full_title = ""
    job.raw_title = job.full_title.split("|")[0].strip()
    if not job.raw_title:
        fail("download", "Could not extract a raw title from: %r" % job.full_title)
    _write_video_meta(job)
    print("Promo title : %s" % job.full_title)
    print("Raw title   : %s" % job.raw_title)


# ---------------------------------------------------------------- Stage 3: hook montage
def _parse_ts(s):
    s = s.strip()
    parts = s.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        fail("montage", "Bad timestamp %r -- use m:ss like 1:10" % s)


def _find_sounding(path, dur):
    """[(start, end)] of non-silent (voice) regions via silencedetect."""
    cmd = ["ffmpeg", "-y", "-v", "info", "-i", path,
           "-af", "silencedetect=noise=-30dB:d=0.5", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    sil, start = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            start = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            sil.append((start, float(m.group(1))))
            start = None
    if start is not None:
        sil.append((start, dur))
    sounding, t = [], 0.0
    for s, e in sil:
        if s - t >= 2.0:
            sounding.append((t, s))
        t = max(t, e)
    if dur - t >= 2.0:
        sounding.append((t, dur))
    return sounding


def _auto_clips(sounding):
    """Walk from 0s: take <=3.8s talking pieces with 5-6s gaps, total <18s."""
    clips, t = [], 0.0
    i = 0
    while len(clips) < HOOK_MAX_CLIPS and i < len(sounding):
        s, e = sounding[i]
        if e <= t + 0.3:      # already passed this region
            i += 1
            continue
        cs = max(s, t)
        cl = min(HOOK_MAX_PIECE, e - cs)
        if cl < HOOK_MIN_PIECE:  # too short to be a real talking piece
            t = e + 1.0
            i += 1
            continue
        trial = clips + [(cs, cs + cl)]
        # xfade overlaps clips, so the final runtime shrinks by fade*(n-1)
        final = sum(e2 - s2 for s2, e2 in trial) - HOOK_FADE * (len(trial) - 1)
        if final >= HOOK_MAX_TOTAL:
            break
        clips.append((cs, cs + cl))
        t = cs + cl + HOOK_GAP  # 5-6s gap before the next piece (never sequential)
        # stay on the same region: it may still hold another piece after the gap
    return clips


def _fallback_clips(dur):
    """Last resort: evenly spaced 3.5s pieces when silence detection finds
    almost nothing (e.g. wall-to-wall music bed)."""
    print("WARNING: little silence found -- using evenly spaced pieces.")
    clips = []
    for frac in (0.06, 0.28, 0.50, 0.72):
        cs = frac * dur
        cl = min(3.5, dur - cs)
        if cl < HOOK_MIN_PIECE:
            continue
        trial = clips + [(cs, cs + cl)]
        final = sum(e - s for s, e in trial) - HOOK_FADE * (len(trial) - 1)
        if final >= HOOK_MAX_TOTAL:
            break
        clips.append((cs, cs + cl))
    return clips


def _fmt_ts(s):
    return "%d:%05.2f" % (int(s // 60), s % 60)


def _build_montage(src, ranges, output):
    durs = [e - s for s, e in ranges]
    # xfade overlaps clips, so the final runtime shrinks by fade*(n-1)
    final_dur = sum(durs) - HOOK_FADE * (len(durs) - 1)
    print("Clips: %d | raw total %.1fs -> final ~%.1fs"
          % (len(durs), sum(durs), final_dur))
    for (s, e) in ranges:
        print("  piece %s - %s (%.1fs)" % (_fmt_ts(s), _fmt_ts(e), e - s))
    if final_dur >= HOOK_MAX_TOTAL:
        fail("montage",
             "Final hook would be %.1fs -- must stay UNDER 18s." % final_dur)

    z = HOOK_ZOOM
    filt = []
    for i, (s, e) in enumerate(ranges):
        filt.append(
            "[0:v]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS,fps=30,"
            "scale=iw*%s:ih*%s,crop=in_w/%s:in_h/%s,"
            "scale=1920:1080,setsar=1[v%d]"
            % (s, e, z, z, z, z, i)
        )
        filt.append("[0:a]atrim=start=%.3f:end=%.3f,asetpts=PTS-STARTPTS[a%d]" % (s, e, i))

    last_v, last_a = "v0", "a0"
    offset = durs[0]
    for i in range(1, len(durs)):
        out_v, out_a = "vx%d" % i, "ax%d" % i
        off = offset - HOOK_FADE
        filt.append("[%s][v%d]xfade=transition=fadeblack:duration=%.3f:offset=%.3f[%s]"
                    % (last_v, i, HOOK_FADE, off, out_v))
        filt.append("[%s][a%d]acrossfade=d=%.3f[%s]" % (last_a, i, HOOK_FADE, out_a))
        last_v, last_a = out_v, out_a
        offset = offset + durs[i] - HOOK_FADE

    filt.append("[%s]format=yuv420p[vout]" % last_v)
    filt.append("[%s]aformat=sample_fmts=fltp:channel_layouts=stereo[aout]" % last_a)

    cmd = ["ffmpeg", "-y", "-i", src,
           "-filter_complex", ";".join(filt),
           "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", output]
    print("Running ffmpeg...")
    run_ffmpeg(cmd, "montage")
    print("Done -> %s (~%.1fs, strictly under 18s)." % (output, final_dur))


def stage_montage(job):
    """Auto-find talking pieces (<=4s, 5-6s gaps, total <18s), zoom to hide
    logos, join with black fades -> montage.mp4."""
    src = job.source_file
    if not os.path.isfile(src):
        fail("montage", "Source video not found: %s" % src)
    dur = ffprobe_duration(src)
    print("Analyzing audio for talking pieces (%.0fs video) ..." % dur)
    sounding = _find_sounding(src, dur)
    print("Found %d voice region(s)." % len(sounding))
    ranges = _auto_clips(sounding)
    if len(ranges) < 3:
        ranges = _fallback_clips(dur)
    if not ranges:
        fail("montage", "Could not find usable talking pieces.")
    print("Auto-selected %d piece(s)." % len(ranges))
    _build_montage(src, ranges, F_MONTAGE)
    need(F_MONTAGE, "montage")


# ---------------------------------------------------------------- Stage 4: screenshots
def stage_scenes(job):
    """Screenshot every scene: lossless PNG, zoomed, 16:9. Rejects near-black,
    near-white and flat logo/title cards automatically."""
    src = job.source_file
    if not os.path.isfile(src):
        fail("scenes", "Source video not found: %s" % src)
    os.makedirs(F_SCENES_DIR, exist_ok=True)
    z = SCENE_ZOOM
    # scale up by z, then crop back: the outer 1-1/z of the picture (where
    # corner logos live) is cut away; the center detail fills the frame.
    vf = ("select='gt(scene,%s)',"
          "scale=iw*%s:ih*%s,crop=in_w/%s:in_h/%s,"
          "scale=%d:%d,format=rgb24" % (SCENE_THRESHOLD, z, z, z, z,
                                        SCENE_WIDTH, SCENE_HEIGHT))
    out = os.path.join(F_SCENES_DIR, "scene_%03d.png")
    print("Extracting scenes from %s ..." % src)
    run_ffmpeg(["ffmpeg", "-y", "-v", "error", "-i", src,
                "-vf", vf] + _vfr_flag() + [out], "scenes")
    # Unzoomed copies for the thumbnail: the zoom crop above cuts ~10% off
    # every edge, which clips edge text like a bottom/corner drama title.
    # The thumbnail must use the full frame (corner logos are inpainted
    # there instead). Same scene numbering, so picks correspond 1:1.
    vf_full = ("select='gt(scene,%s)',scale=%d:%d,format=rgb24"
               % (SCENE_THRESHOLD, SCENE_WIDTH, SCENE_HEIGHT))
    out_full = os.path.join(F_SCENES_DIR, "fullscene_%03d.png")
    run_ffmpeg(["ffmpeg", "-y", "-v", "error", "-i", src,
                "-vf", vf_full] + _vfr_flag() + [out_full], "scenes")
    shots = sorted(f for f in os.listdir(F_SCENES_DIR)
                   if f.startswith("scene_") and f.endswith(".png"))
    print("Extracted %d candidates, filtering bad frames ..." % len(shots))

    try:
        import cv2
        import numpy as np
    except ImportError:
        fail("scenes", "opencv-python not installed. Run: pip install opencv-python")

    kept = []
    dropped = {"black": 0, "white flash": 0, "logo/title card": 0}

    def _drop_pair(path):
        for p in (path, os.path.join(
                F_SCENES_DIR, os.path.basename(path).replace(
                    "scene_", "fullscene_", 1))):
            try:
                os.remove(p)
            except OSError:
                pass

    for f in shots:
        path = os.path.join(F_SCENES_DIR, f)
        gray = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            dropped["black"] += 1
            _drop_pair(path)
            continue
        mean = float(gray.mean())
        if mean < REJECT_BLACK:
            dropped["black"] += 1
            _drop_pair(path)
            continue
        if mean > REJECT_WHITE:
            dropped["white flash"] += 1
            _drop_pair(path)
            continue
        med = float(np.median(gray))
        flatfrac = float(np.mean(np.abs(gray.astype(np.int16) - med)
                                <= REJECT_FLAT_TOL))
        if flatfrac > REJECT_FLAT_FRAC:
            dropped["logo/title card"] += 1
            _drop_pair(path)
            continue
        kept.append(f)

    # renumber the survivors scene_001.png, scene_002.png, ... (and the
    # matching fullscene_ files identically)
    kept.sort()
    for i, f in enumerate(kept):
        os.rename(os.path.join(F_SCENES_DIR, f),
                  os.path.join(F_SCENES_DIR, "_tmp_%03d.png" % (i + 1)))
        ff = f.replace("scene_", "fullscene_", 1)
        os.rename(os.path.join(F_SCENES_DIR, ff),
                  os.path.join(F_SCENES_DIR, "_ftmp_%03d.png" % (i + 1)))
    for i in range(len(kept)):
        os.rename(os.path.join(F_SCENES_DIR, "_tmp_%03d.png" % (i + 1)),
                  os.path.join(F_SCENES_DIR, "scene_%03d.png" % (i + 1)))
        os.rename(os.path.join(F_SCENES_DIR, "_ftmp_%03d.png" % (i + 1)),
                  os.path.join(F_SCENES_DIR, "fullscene_%03d.png" % (i + 1)))

    print("Dropped: " + ", ".join("%d %s" % (v, k) for k, v in dropped.items()))
    if not kept:
        fail("scenes", "No usable screenshots survived filtering.")
    print("Done -> %d clean screenshots in %s/" % (len(kept), F_SCENES_DIR))


# ---------------------------------------------------------------- Stage 5: transcription
def stage_transcribe(job):
    """Transcribe the trailer audio to Turkish text (faster-whisper, local)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        fail("transcribe", "faster-whisper not installed. Run: pip install faster-whisper")
    src = job.source_file
    if not os.path.isfile(src):
        fail("transcribe", "Source video not found: %s" % src)
    print("Loading model '%s' (first run downloads it, ~500 MB) ..." % WHISPER_MODEL)
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    print("Transcribing %s ..." % src)
    segments, _info = model.transcribe(src, language=WHISPER_LANG)
    lines = [seg.text.strip() for seg in segments if seg.text.strip()]
    if not lines:
        fail("transcribe", "Transcription produced no text.")
    with open(F_TRANSCRIPT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("Done -> %s (%d lines)" % (F_TRANSCRIPT, len(lines)))
    need(F_TRANSCRIPT, "transcribe")


# ---------------------------------------------------------------- Stage 6: narration script
# Deterministic narration builder: writes the 7-8 minute Turkish narration
# ITSELF from the transcript -- no AI service, instant, can never fail or get
# stuck. Rotating templates (seeded per drama) keep every video's narration
# varied; paragraph depth adapts to the transcript length so the total always
# lands at 900-1100 words.

_NARR_INTROS = [
    "{label} yayınlandı ve daha ilk saniyeden itibaren tansiyon hiç düşmüyor. "
    "Bu tanıtım, izleyen herkesi şoke eden anlarla dolu. Bu videoda fragmanı sahne sahne "
    "inceliyor, her repliğin ardındaki gerçeği ortaya çıkarıyoruz. Kimi zaman bir tehdit "
    "duyacağız, kimi zaman bir itiraf, kimi zaman da yüreğimizi burkan bir fedakârlık hikâyesi. "
    "Ama her cümlenin ardında aynı soru gizli: Bu işin sonu nereye varacak? Kemerleri bağlayın, "
    "çünkü bu fragmanda hiçbir şey göründüğü gibi değil.",

    "Yeni fragman geldi ve ortalık karıştı. {drama}{ep_txt}, {frag_txt} ile nefesleri kesiyor. "
    "Bu videoda, fragmandaki her sahneyi tek tek ele alıyor, söylenen her sözün ardındaki anlamı "
    "çözüyoruz. Sırlar, tehditler, pişmanlıklar ve büyük bir fedakârlık... Hepsi bu kısa tanıtımın "
    "içine sığmış. Peki bu olaylar zinciri bizi nereye götürecek? Gelin, bu gerilim dolu fragmanın "
    "şifrelerini birlikte çözelim.",
]

_NARR_BODY_MED = [
    'Sahne değişir ve gerilim bir anda yükselir. Net, sarsıcı bir ses şunları söyler: "{q}" '
    'Bu cümle sıradan bir replik değil; ardında saklanan anlamı düşündükçe tüylerimiz ürperiyor. '
    'Peki bu sözler kime söylendi? Ve ardındaki gerçek niyet ne? Fragman bu soruları cevapsız bırakıyor.',

    'Derin bir sessizliğin ardından gelen bu replik, fragmanın en çarpıcı anlarından biri: "{q}" '
    'Bu sözleri söyleyen kişinin sesindeki ton her şeyi anlatıyor. Öfke mi, korku mu, yoksa soğuk bir '
    'kararlılık mı? Bu cümle, hikâyenin yönünü belirleyecek gibi duruyor. Acaba bir başlangıcın mı, '
    'yoksa bir sonun mu habercisi?',

    'Kamera bir yüze odaklanır ve zaman durur: "{q}" Bu replik, fragmanın fısıldadığı en büyük '
    'sırlardan birini taşıyor. Söylenmeyenler, söylenenlerden çok daha fazlasını anlatıyor. '
    'Peki bu hikâyenin sonu nereye varacak? Bu sorunun cevabını öğrenmek için yeni bölümü beklemek zorundayız.',

    'Fragmanın bu anında tansiyon zirveye çıkar: "{q}" Bu sözler, buz gibi bir gerçekliği yüzümüze '
    'çarpıyor. Söyleyen kişi ne hissettiğini gizlemiyor ve bu dürüstlük, durumu daha da ürkütücü kılıyor. '
    'Bu cümlenin ardında hangi yaşanmışlık var? Ve bundan sonra neler olacak?',

    'Dikkatle dinleyin, çünkü bu replik çok şey anlatıyor: "{q}" Her kelime özenle seçilmiş gibi. '
    'Bu, anlık bir öfkenin değil, uzun uzun düşünülmüş bir kararın sesi. Peki bu kararı aldıran neydi? '
    'Ve bu karar, hikâyedeki dengeleri nasıl değiştirecek?',

    'Fragman bizi bu sahnede bir anlığına durduruyor: "{q}" Bu cümleyi kuran kişinin gözlerindeki ifade, '
    'kelimelerin ötesinde bir hikâye anlatıyor. Acı mı, pişmanlık mı, yoksa gizli bir plan mı? '
    'Belki de üçü birden. Bu an, yeni bölümün en kritik sahnelerinden birinin habercisi olabilir.',

    'İşte fragmanın en çok konuşulacak repliklerinden biri: "{q}" Bu sözler, hikâyenin tam kalbine dokunuyor. '
    'Söyleyen kişi, bütün duygularını bu tek cümleye sığdırmış. Peki bu duyguların hedefi kim? '
    'Ve bu itiraf, olayların akışını nasıl değiştirecek? Sorular birikiyor, cevaplar yeni bölümde.',

    'Bu sahnede nefesler tutulur: "{q}" Kısa ama etkili. Bazen en az kelime, en çok anlamı taşır. '
    'Bu replik de öyle. Ardında büyük bir hikâye, büyük bir sır ve büyük bir karar var. '
    'Peki bu kararın bedelini kim ödeyecek? Fragman susuyor, ama gerilim konuşuyor.',

    'Fragmanın bu bölümü, izleyeni derinden sarsıyor: "{q}" Bu sözler, bir dönüm noktasının habercisi gibi. '
    'Söyleyen kişi, bir eşiği aşmış görünüyor. Artık geri dönüş yok. Peki bu eşik, onu kurtuluşa mı '
    'götürecek, yoksa felakete mi? Bu sorunun cevabı, hepimizi ekrana kilitleyecek.',

    'Ve sonra bu cümle gelir: "{q}" Fragmanın en gizemli anlarından biri. Her dinleyişte farklı bir anlam '
    'çıkıyor. Tek bir cümle, onlarca soru doğurur. Peki gerçek cevap hangisi? Bunu ancak yeni bölüm gösterecek.',
]

_NARR_BODY_SHORT = [
    'Kısa ama sarsıcı bir an: "{q}" Bu cümle, fragmanın gerilimini bir anda katlıyor. Peki ardında ne var?',

    'Bu replik çok şey anlatıyor: "{q}" Söyleyenin niyeti ne? Bu soru, fragman boyunca peşimizi bırakmıyor.',

    'Dikkat çeken bir sahne: "{q}" Bu sözler, hikâyenin yönünü değiştirebilir. Bekleyip göreceğiz.',

    'Fragmanın çarpıcı anlarından: "{q}" Bu cümlenin ağırlığı, sahneye damgasını vuruyor.',

    'Bir itiraf gibi gelen bu sözler: "{q}" Peki bu itirafın bedeli ne olacak?',

    'Gerilimi tırmandıran replik: "{q}" Bu sözler, yeni bölümde büyük olayların habercisi.',
]

_NARR_BODY_EXTRA = [
    'Bu repliği bir kez daha düşünelim. Söyleniş biçimi, kelime seçimi, zamanlaması... Hepsi bir şey anlatıyor. '
    'Bu kişi, bu sözleri söylerken ne hissediyordu? Korku mu, öfke mi, yoksa çaresizlik mi? Cevap ne olursa olsun, '
    'bu cümlenin hikâyede büyük bir karşılığı olacağı kesin.',

    'Peki bu sözlerin muhatabı kim? Fragman bize bunu göstermiyor, ama ipuçları var. Sesin tonu, sahnenin atmosferi... '
    'Hepsi bir hedefi işaret ediyor. Ve o hedef, bu sözleri duyduğunda ne yapacak? İşte asıl merak edilen bu.',

    'Bu tür replikler, fragmanların en sevdiği tuzaklardır. Bize bir şey gösterir ama gerçeği gizler. Acaba bu cümle, '
    'göründüğü gibi mi, yoksa ardında bambaşka bir anlam mı var? Bu soruyu aklımızın bir köşesine yazalım.',

    'Bu cümlenin hikâyedeki yeri çok kritik. Öncesinde ve sonrasında yaşananlar, bu sözlere bambaşka anlamlar katıyor. '
    'Bir cümle, bağlamıyla yaşar. Ve bu bağlam, her geçen sahnede biraz daha netleşiyor.',

    'Şunu da unutmamak gerek: Bu sözleri söyleyen kişi, bu noktaya kolay gelmedi. Ardında yaşanmışlıklar, kırgınlıklar, '
    'belki de affedilmemiş hatalar var. Bu replik, bir anın değil, bir geçmişin özeti.',

    'Bu replikten sonra fragmanın ritmi değişiyor. Artık her sahne biraz daha karanlık, her bakış biraz daha anlamlı. '
    'Tek bir cümleyle bütün atmosfer değişir. Peki bu karanlık nereye varacak?',
]

_NARR_DEEP = [
    'Biraz geri çekilip büyük resme bakalım. Bu fragmanın bütün sahnelerinde ortak bir duygu var: güven sarsıntısı. '
    'Kimse kimseye tam olarak güvenmiyor. Söylenen her sözün ardında bir "acaba" gizli. Güven sarsıldığında, her ilişki '
    'bir satranç oyununa döner. Ve bu fragmanda, satranç tahtasındaki taşlar çoktan dizilmiş. Peki ilk hamleyi kim yapacak?',

    'Bu fragmanda dikkat çeken bir diğer şey, sırların ağırlığı. Herkes bir şey saklıyor. Kimi geçmişini, kimi niyetini, '
    'kimi de korkularını. Sırlar, bu tür hikâyelerde saatli bomba gibidir. Ne zaman patlayacağı bilinmez, ama patlayacağı '
    'kesindir. Fragman, bu bombalardan birkaçının fitilinin ateşlendiğini gösteriyor. Peki hangisi önce patlayacak?',

    'Fedakârlık teması, bu fragmanın en güçlü damarı. "{anchor}" Bu cümle, hikâyenin duygusal merkezini oluşturuyor. '
    'Bir insan, sevdiği biri için nelerden vazgeçebilir? Bu sorunun cevabı bu hikâyede aranıyor. Ama fedakârlığın da bir '
    'bedeli var. Ve o bedel ödendiğinde, geriye ne kalır?',

    'İntikam duygusu da fragmanın satır aralarında geziniyor. Ödenmemiş hesaplar, affedilmemiş hatalar, yarım kalmış '
    'yüzleşmeler... Bunların hepsi bir patlama noktasına doğru ilerliyor. İntikam, bu tür hikâyelerde hem yakıttır hem de '
    'ateş. Karakterleri harekete geçirir, ama aynı zamanda onları yakar. Peki bu ateş kimi yakacak?',

    'Aile bağları, bu fragmanın görünmez kahramanı. Kan bağı, bu hikâyede hem en güçlü kalkan hem de en keskin kılıç. '
    'Aile için yapılanlar, aileye rağmen yapılanlar... Bu ikilem her sahnede kendini gösteriyor. Peki kan mı kazanacak, '
    'yoksa vicdan mı? Bu sorunun cevabı, hikâyenin finalini belirleyecek.',

    'Fragmanın kurgusuna dikkat edelim. Sahneler özenle seçilmiş ve her biri bir sonrakini hazırlıyor. Önce bir itiraf, '
    'sonra bir tehdit, sonra bir merak, sonra bir pişmanlık... Bu ritim, izleyiciyi nefessiz bırakmak için tasarlanmış. '
    'Ve işe yarıyor. Çünkü her sahne, bir öncekinden biraz daha karanlık. Bu tırmanışın zirvesi, yeni bölümde olacak.',

    'Karakterlerin yüz ifadelerine odaklanalım. Fragman bize çok şeyi kelimelerle değil, bakışlarla anlatıyor. Korku, öfke, '
    'pişmanlık, kararlılık... Bütün bu duygular tek bir karede okunabiliyor. İyi bir fragman böyle çalışır: gösterir ama '
    'anlatmaz. Ve biz, gösterilenlerden anlatılmayanı çıkarmaya çalışırız.',

    'Son olarak şunu söyleyelim: Bu fragman bir vaat gibi. Bize diyor ki, yeni bölümde büyük şeyler olacak. Dengeler '
    'değişecek, maskeler düşecek, sırlar açığa çıkacak. Ve biz, bütün bu vaatlerin tutulup tutulmadığını görmek için ekran '
    'başında olacağız. Çünkü bu hikâye henüz bitmedi. Aslında daha yeni başlıyor.',

    'Fragmanın ses tasarımına kulak verelim. Müzik, sessizlik anları, yükselen tonlar... Bunların hepsi duygularımızı '
    'yönetmek için seçilmiş. Gerilim anında hızlanan ritim, duygusal sahnede yumuşayan melodiler. Bu, fragmanın görünmez '
    'yönetmenidir. Ve bu yönetmen, bizi tam istediği duyguya sürüklüyor. Peki yeni bölümde bu müzik, hangi sahnede zirveye çıkacak?',

    'Karakter gelişimine odaklanalım. Fragmandaki her replik, bir karakterin yolculuğunda bir kilometre taşı. Kimisi öfkeye, '
    'kimisi pişmanlığa, kimisi de kararlılığa doğru ilerliyor. Bu yolculukların kesiştiği nokta, hikâyenin kalbi. Ve o kalp, '
    'yeni bölümde daha hızlı atacak. Hangi karakterin dönüşümü bizi en çok şaşırtacak?',

    'Öngörüler ve ipuçları... Fragman, dikkatli izleyiciye küçük hediyeler bırakır. Arka planda görünen bir detay, yarım '
    'duyulan bir cümle, bir bakışın yönü... Bunların hepsi, gelecek bölümlerin şifreleri olabilir. Biz bu şifreleri çözmeye '
    'çalıştık. Peki siz, fragmanda bizim kaçırdığımız bir detay fark ettiniz mi?',

    'Merkezdeki çatışmaya bakalım. Bu hikâyede iki güç karşı karşıya geliyor. Bir yanda duygular, diğer yanda gerçekler. '
    'Bir yanda sadakat, diğer yanda hayatta kalma içgüdüsü. Bu çatışma, her sahnede biraz daha keskinleşiyor. Ve keskinleşen '
    'her çatışma, bir patlamaya gebedir. O patlama, yeni bölümde yaşanacak.',

    'Tehlikede olan şey ne? Her iyi hikâyede, karakterlerin kaybedecek bir şeyi vardır. Burada da öyle. Kimi ailesini, '
    'kimi onurunu, kimi de geleceğini riske atıyor. Tehlike ne kadar büyükse, gerilim de o kadar büyük olur. Ve bu fragmanda '
    'tehlike, her sahnede biraz daha büyüyor.',

    'Zamanlama da bir karakterdir bu fragmanda. Geçmişin gölgesi, şimdinin gerilimi, geleceğin belirsizliği... Üç zaman '
    'dilimi, tek bir tanıtımda buluşuyor. Geçmişte yaşananlar bugünü şekillendiriyor, bugün yaşananlar geleceği belirliyor. '
    'Bu zincirin halkaları, yeni bölümde tek tek kırılacak mı, yoksa daha da mı güçlenecek?',

    'İzleyici olarak bizim rolümüzü de unutmayalım. Fragman, bizi pasif bir izleyici değil, aktif bir dedektif yapıyor. '
    'İpuçlarını birleştiriyor, teoriler üretiyor, sonuçlar çıkarıyoruz. Ve bu, bu tür videoların en güzel yanı. Hep birlikte '
    'bu gizemi çözmeye çalışıyoruz. Peki sizin teoriniz ne?',

    'Son bir not: Fragmanlar bazen yanıltır. Gösterdikleri, gerçeğin ta kendisi olmayabilir. Bağlamından koparılmış bir '
    'sahne, bambaşka bir anlam taşıyabilir. Bu yüzden, bu analizdeki her yorumu bir ihtimal olarak alın. Gerçek, yeni '
    'bölümde ortaya çıkacak. Ve o gerçek, tahminlerimizden çok daha şaşırtıcı olabilir.',
]

_NARR_OUTROS = [
    'Fragman biterken aklımızda onlarca soru kalıyor. Sırlar açığa çıkacak mı? Tehditler gerçekleşecek mi? '
    'Fedakârlıklar karşılığını bulacak mı? Ve en önemlisi: Bu hikâyenin sonunda kim ayakta kalacak? Bütün bu soruların '
    'cevapları, yeni bölümde tek tek ortaya çıkacak. Biz, o cevapları öğrenmek için sabırsızlanıyoruz. '
    'Yeni gelişmeler için takipte kalın.',

    'Ve fragman burada bitiyor, ama hikâye daha yeni başlıyor. Aklımızda kalan sorular: Bu gerilimin sonu nereye varacak? '
    'Kim, kime, neyin bedelini ödetecek? Ve bu fedakârlıkların sonunda kim kazanacak? Cevaplar, yeni bölümde bizi bekliyor. '
    'Bu fragman analizi burada sona eriyor. Kaçırmamak için takipte kalın.',
]


def _build_narration(transcript_text, job, target_words):
    """Write the narration deterministically from the transcript.

    No AI service: instant, free, and always near target_words (so the
    final video lands in the 7-8 minute band). Template rotation is
    seeded per drama so every video reads differently; paragraph depth
    adapts to the transcript length."""
    drama_full, drama, ep, frag, hook = _parse_title_parts(job)
    if not drama:
        drama = drama_full or "Bu dizi"
    ep_txt = ("%s. Bölüm" % ep) if ep else ""
    frag_txt = ("fragmanın" if not frag else "%s. fragmanı" % frag)
    label_bits = [x for x in [drama, ep_txt] if x]
    label = " ".join(label_bits)
    if frag:
        label += " %s. fragmanı" % frag
    else:
        label += " fragmanı"
    if hook:
        label += ' ("%s")' % hook

    lines = [ln.strip().replace("{", "").replace("}", "")
             for ln in transcript_text.split("\n") if ln.strip()]
    quotes = [ln for ln in lines if len(ln.split()) >= 3] or lines
    if len(quotes) > 40:
        # Very chatty trailer: keep the 40 most substantial lines, in order.
        keep = set(sorted(range(len(quotes)),
                          key=lambda i: len(quotes[i]), reverse=True)[:40])
        quotes = [q for i, q in enumerate(quotes) if i in keep]
    # Stable per-drama rotation offset (no import needed, deterministic).
    seed = sum(bytearray(drama.encode("utf-8")))

    paras = [_NARR_INTROS[seed % len(_NARR_INTROS)].format(
        label=label, drama=drama, ep_txt=(" %s" % ep_txt) if ep_txt else "",
        frag_txt=frag_txt)]

    n = len(quotes)
    per = (target_words - 160.0) / max(n, 1)
    for i, q in enumerate(quotes):
        if per > 80:
            p = _NARR_BODY_MED[(seed + i) % len(_NARR_BODY_MED)].format(q=q)
            p += " " + _NARR_BODY_EXTRA[(seed + i) % len(_NARR_BODY_EXTRA)]
        elif per > 40:
            p = _NARR_BODY_MED[(seed + i) % len(_NARR_BODY_MED)].format(q=q)
        else:
            p = _NARR_BODY_SHORT[(seed + i) % len(_NARR_BODY_SHORT)].format(q=q)
        paras.append(p)

    # Deep-dive padding if the transcript was short (kept in logical order).
    anchor = max(quotes, key=len) if quotes else ""
    words = sum(len(p.split()) for p in paras)
    di = 0
    while words < target_words - 60 and di < len(_NARR_DEEP):
        paras.append(_NARR_DEEP[di].format(anchor=anchor))
        words = sum(len(p.split()) for p in paras)
        di += 1

    paras.append(_NARR_OUTROS[seed % len(_NARR_OUTROS)])
    return "\n\n".join(paras)


_SCRIPT_PROMPT = """You are the narrator of a Turkish TV drama trailer breakdown video.
Below is the transcript of a drama trailer. Write a suspenseful third-person
Turkish narration script that explains and analyzes this trailer scene by scene,
exactly like a TV drama recap channel.

Rules:
- Turkish language only.
- {wmin} to {wmax} words (this becomes about 7-7.5 minutes of voiceover).
- Suspenseful, dramatic tone. Build tension, tease unanswered questions.
- Walk through the trailer's key moments in order, quoting the most dramatic
  dialogue lines and explaining what they reveal.
- End with cliffhanger questions ("Kim geri adim atacak? Kim direnecek?" style)
  and one line saying the answers will come in the new episode.
- Plain narration text only: no stage directions, no timestamps, no headings,
  no bullet points. Paragraphs separated by blank lines.

Transcript:
---
{transcript}
---
"""

_SCRIPT_CONTINUE_PROMPT = """You are continuing a Turkish TV drama trailer narration script.
Here is the script so far:
---
{script}
---
Continue the narration in the same suspenseful third-person Turkish style.
Add 150-250 more words: analyze the trailer's remaining key scenes more deeply,
quote more dramatic dialogue lines, raise new cliffhanger questions.
Plain narration text only: no headings, no bullet points, no repeated ending.
Keep building tension."""


def _extend_script(script, deadline, floor, max_rounds=SCRIPT_EXTEND_ROUNDS):
    """If the script is shorter than the word floor, ask the free AI to
    continue it (up to max_rounds rounds) so the voiceover reaches the
    7-8 minute band. Stops early if the AI time budget is exhausted."""
    words = len(script.split())
    rounds = 0
    while words < floor and rounds < max_rounds:
        if time.time() > deadline:
            print("AI time budget exhausted, stopping extensions.")
            break
        print("Script is %d words, extending (round %d/%d) ..."
              % (words, rounds + 1, max_rounds))
        prompt = _SCRIPT_CONTINUE_PROMPT.format(script=script[-6000:])
        extra = free_generate(prompt, max_tokens=SCRIPT_EXTEND_TOKENS,
                              temperature=0.8)
        if not extra:
            print("Extension failed, keeping current script.")
            break
        script = script.rstrip() + "\n\n" + extra.strip()
        words = len(script.split())
        rounds += 1
    return script


def _trim_to_max_words(script, max_words, min_words):
    """Trim a too-long script to the last complete sentence within max_words,
    so the voiceover stays inside the 7-8 minute band. Never trims below
    min_words: if no safe sentence cut exists above the floor, the script
    is returned unchanged with a warning."""
    words = script.split()
    if len(words) <= max_words:
        return script
    cut = " ".join(words[:max_words])
    best = -1
    for m in re.finditer(r'[.!?\u2026]["\u2019\']?(?=\s|$)', cut):
        best = m.end()
    if best > 0:
        trimmed = cut[:best].strip()
        if len(trimmed.split()) >= min_words:
            print("Script was %d words, trimmed to %d words "
                  "(stays within the duration band)." % (len(words),
                                                         len(trimmed.split())))
            return trimmed
    print("WARNING: script is %d words (over the %d-word ceiling) "
          "but has no safe sentence cut; keeping it whole."
          % (len(words), max_words))
    return script


def stage_script(job):
    """Turn the transcript into a narration script sized so the FINAL video
    lands in the 7-8 minute band. The word target is computed from the
    measured TTS rate on this PC. The free keyless AI is tried first; if it
    cannot deliver, the built-in deterministic writer generates the narration
    itself from the transcript -- so EVERY video gets its script
    automatically. Scripts over the ceiling are trimmed to the last full
    sentence. With --keep-script, your own script.txt is used instead."""
    floor, ceil = _script_targets()
    wpm = _tts_wpm()
    print("Script target: %d-%d words (~%.1f min voiceover at %.0f wpm)"
          % (floor, ceil, ((floor + ceil) / 2) / wpm, wpm))
    if getattr(job, "keep_script", False):
        if not os.path.isfile(F_SCRIPT):
            fail("script", "--keep-script was given but %s was not found. "
                           "Save your narration script as %s next to "
                           "diziverse.py, then run again." % (F_SCRIPT, F_SCRIPT))
        with open(F_SCRIPT, encoding="utf-8") as f:
            script = f.read().strip()
        words = len(script.split())
        if words < floor:
            fail("script", "Your %s is only %d words (~%.1f min) -- below the "
                           "%d-word minimum for the 7-8 minute band. Make it "
                           "longer, then run again."
                 % (F_SCRIPT, words, words / wpm, floor))
        print("Using your %s (~%d words, roughly %.1f min of voiceover)"
              % (F_SCRIPT, words, words / wpm))
        if words > ceil:
            print("NOTE: your %s is %d words, over the duration band -- "
                  "the voiceover stage will trim it to fit." % (F_SCRIPT, words))
        need(F_SCRIPT, "script")
        return
    if not os.path.isfile(F_TRANSCRIPT):
        fail("script", "Transcript not found: %s" % F_TRANSCRIPT)
    with open(F_TRANSCRIPT, encoding="utf-8") as f:
        transcript = f.read().strip()
    if not transcript:
        fail("script", "Transcript is empty.")
    t_start = time.time()
    deadline = t_start + SCRIPT_AI_BUDGET
    script = free_generate(_SCRIPT_PROMPT.format(transcript=transcript,
                                                 wmin=floor, wmax=ceil),
                           max_tokens=SCRIPT_MAX_TOKENS, temperature=0.8)
    words = len(script.split()) if script else 0
    if words >= floor:
        if words > ceil:
            script = _trim_to_max_words(script, ceil, floor)
            words = len(script.split())
        print("Free AI wrote the full script (~%d words)." % words)
    else:
        if words >= 300 and time.time() < deadline:
            # The AI is trying but short: one extension attempt.
            print("AI script is %d words, one extension attempt ..." % words)
            script = _extend_script(script, deadline, floor, max_rounds=1)
            words = len(script.split())
        if words < floor:
            # Free AI cannot deliver today: generate the narration
            # deterministically from the transcript. Instant, always
            # near the target.
            print("Free AI gave only %d words; the built-in writer is "
                  "generating the narration ..." % words)
            script = _build_narration(transcript, job, (floor + ceil) // 2)
            words = len(script.split())
    if words > ceil:
        # Hard ceiling: trim to the last full sentence so the video
        # stays inside the 7-8 minute band.
        script = _trim_to_max_words(script, ceil, floor)
        words = len(script.split())
    if words < floor:
        # Hard gate: never build/upload a video outside the duration band.
        fail("script",
             "Could not produce a %d-word narration (%d words). "
             "No video was built."
             % (floor, words))
    with open(F_SCRIPT, "w", encoding="utf-8") as f:
        f.write(script + "\n")
    words = len(script.split())
    print("Done -> %s (~%d words, roughly %.1f min of voiceover)"
          % (F_SCRIPT, words, words / wpm))
    need(F_SCRIPT, "script")


# ---------------------------------------------------------------- Stage 7: voiceover
def _chunk_text(text, limit):
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 > limit and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + "\n" + p).strip()
    if cur:
        chunks.append(cur)
    return chunks


async def _tts_chunk(text, voice, path):
    import edge_tts
    comm = edge_tts.Communicate(text, voice)
    await comm.save(path)


def _synthesize_voiceover(text):
    """Synthesize the narration text to voice.mp3. Returns duration (sec)."""
    chunks = _chunk_text(text, VOICE_CHUNK)
    print("Generating voiceover: %d chunk(s), voice %s ..."
          % (len(chunks), VOICE_NAME))
    tmpdir = tempfile.mkdtemp(prefix="voice_")

    async def _one(i, ch):
        part = os.path.join(tmpdir, "part_%02d.mp3" % i)
        await _tts_chunk(ch, VOICE_NAME, part)
        print("  chunk %d/%d done" % (i + 1, len(chunks)))
        return part

    async def _all():
        # chunks synthesize concurrently, not one-by-one
        return await asyncio.gather(
            *[_one(i, ch) for i, ch in enumerate(chunks)])

    parts = list(asyncio.run(_all()))
    if len(parts) == 1:
        shutil.move(parts[0], F_VOICE)
    else:
        lst = os.path.join(tmpdir, "list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for p in parts:
                f.write("file '%s'\n" % p.replace("'", "'\\''"))
        run_ffmpeg(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", F_VOICE], "voiceover")
    total = ffprobe_duration(F_VOICE)
    print("Voiceover -> %s (%.1f sec)" % (F_VOICE, total))
    need(F_VOICE, "voiceover")
    return total


def stage_voiceover(job):
    """Turkish MALE AI voiceover of the narration script. No subtitles --
    the final video has no captions anywhere. The measured TTS rate is
    saved so the next script is sized correctly, and a hard gate trims +
    re-synthesizes once if the voiceover would push the final video past
    8 minutes."""
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        fail("voiceover", "edge-tts not installed. Run: pip install edge-tts")
    if not shutil.which("ffmpeg"):
        fail("voiceover", "ffmpeg not found.")
    if not os.path.isfile(F_SCRIPT):
        fail("voiceover", "Script not found: %s" % F_SCRIPT)
    with open(F_SCRIPT, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        fail("voiceover", "Script is empty.")
    total = _synthesize_voiceover(text)
    words = len(text.split())
    if total > 0 and words > 0:
        measured = words / (total / 60.0)
        _save_tts_wpm(measured)
        print("Measured TTS rate: %.0f wpm (saved for the next run)."
              % measured)
    if total > VOICE_MAX_S:
        # Hard gate: the final video must never pass 8 minutes. Trim the
        # script to fit and synthesize once more, using this run's own
        # measured rate so the second pass lands exactly.
        keep = int(words * VOICE_TRIM_S / total)
        print("Voiceover is %.0fs (cap %.0fs): trimming script to ~%d words "
              "and re-synthesizing once ..." % (total, VOICE_MAX_S, keep))
        trimmed = _trim_to_max_words(text, keep, max(1, keep - 120))
        with open(F_SCRIPT, "w", encoding="utf-8") as f:
            f.write(trimmed + "\n")
        total = _synthesize_voiceover(trimmed)
        if total > VOICE_MAX_S:
            fail("voiceover",
                 "Voiceover still %.0fs after trimming -- the video would "
                 "exceed 8 minutes. No video was built." % total)
    if total < 390:
        print("WARNING: voiceover is only %.0fs; the final video may run "
              "under 7 minutes." % total)


# ---------------------------------------------------------------- Stage 8: assembly
_USE_QSV = False  # set at the start of stage_assemble (test encode)


def _qsv_available():
    """True if this FFmpeg can really hardware-encode (tiny test encode)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y",
             "-f", "lavfi", "-i", "nullsrc=s=64x64:r=5:d=1",
             "-c:v", "h264_qsv", "-f", "null", "-"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _venc(quality=ENC_QUALITY):
    """Video codec args: Quick Sync when available, else libx264."""
    if _USE_QSV:
        return ["-c:v", "h264_qsv", "-preset", "veryfast",
                "-global_quality", str(quality)]
    return ["-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(quality)]


def _ember_params():
    """The ember definitions (one shared seed, so the pre-rendered loop
    looks exactly the same on every video)."""
    rng = random.Random(EMBER_SEED)
    ps = []
    for _ in range(EMBER_COUNT):
        ps.append({
            "x": rng.randint(60, OUT_W - 60),
            "size": rng.randint(30, 56),
            "speed": rng.randint(EMBER_SPEED - 7, EMBER_SPEED + 7),
            "offset": rng.randint(0, 800),
            "phase": rng.randint(0, 6),
        })
    return ps


def _find_scene_files():
    """Scene screenshots (zoomed), preferred lossless PNG, falling back to JPG."""
    pngs = sorted(glob.glob(os.path.join(F_SCENES_DIR, "scene_*.png")))
    if pngs:
        return pngs
    return sorted(glob.glob(os.path.join(F_SCENES_DIR, "scene_*.jpg")))


def _find_fullscene_files():
    """Unzoomed scene screenshots for the thumbnail: the full frame, so
    edge text like a bottom/corner drama title is never clipped by the
    zoom crop (corner logos are inpainted instead). Falls back to the
    zoomed set when absent (runs from before this existed)."""
    fulls = sorted(glob.glob(os.path.join(F_SCENES_DIR, "fullscene_*.png")))
    if fulls:
        return fulls
    return _find_scene_files()


def _build_fog(path):
    """Pre-render a loopable cloudy fog layer (cached), bottom-weighted.

    Rendered FOG_PAD_X/FOG_PAD_Y larger than the frame so the drift in
    _build_atmosphere (centered on the padding) never uncovers a bare
    edge -- previously the 1920x1080 layer drifted up to 110px left and
    exposed a moving fogless strip on the right/bottom edges.

    A living alpha mask is baked in: a vertical gradient (transparent
    above ~48% height, full at the bottom) whose top edge slowly breathes
    up and down, multiplied by a soft horizontal source bump slightly left
    of center that wanders gently -- so the dhuwan feels like light smoke
    rising from somewhere, not a flat band. The noise is generated at
    quarter resolution and upscaled for large soft billows (real
    cloud-like texture, not a flat veil), and tblend smooths it over time
    so the fog billows naturally instead of flickering frame-to-frame.
    The composite's Atmosphere 0.25 still scales the overall opacity."""
    if os.path.isfile(path):
        return
    print("Rendering fog layer (one-time) ...")
    W, H = OUT_W + FOG_PAD_X, OUT_H + FOG_PAD_Y
    run_ffmpeg(["ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i",
                "nullsrc=s=%dx%d:r=%d:d=%d" % (W, H, OUT_FPS, ATMOS_LOOP),
                "-f", "lavfi", "-i",
                "nullsrc=s=%dx%d:r=%d:d=%d,format=gray,"
                "geq=lum='255*pow(min(max((Y-(0.48+0.03*sin(0.140*T+0.7))*H)/"
                "((1-(0.48+0.03*sin(0.140*T+0.7)))*H),0),1),1.5)*"
                "exp(-pow(X-W*(0.42+0.05*sin(0.140*T+2.0)),2)/"
                "(2*pow(0.42*W,2)))'"
                % (W, H, OUT_FPS, ATMOS_LOOP),
                "-filter_complex",
                ("[0:v]noise=alls=30:allf=t,"
                 "scale=%d:%d:flags=bilinear,scale=%d:%d:flags=bilinear,"
                 "tblend=all_mode=average,"
                 "eq=brightness=0.45:saturation=0,"
                 "boxblur=luma_radius=16:luma_power=2,format=rgba[n];"
                 "[n][1:v]alphamerge" % (W // 4, H // 4, W, H)),
                "-frames:v", str(ATMOS_LOOP * OUT_FPS),
                "-c:v", "qtrle", path], "assemble")


def _build_loom_fog(path):
    """Pre-render a loopable bottom-weighted fog layer (cached).

    NOTE (2026-09-20): currently unused -- the looming fog was removed
    from the atmosphere per user request (smog effect only). Kept so it
    can be re-enabled without rewriting.

    A soft cloud-like veil hugging the bottom of the frame with a smooth
    absorbed gradient (never a hard band): transparent in the upper half,
    ramping to full density at the bottom edge. The noise is generated at
    1/16 resolution and upscaled, so the fog forms large soft billows like
    natural clouds instead of fine grain; it is temporally smoothed
    (tblend) so the billows morph slowly instead of flickering, and it
    drifts laterally on two slow harmonics (Speed 4) like real low cloud.
    The layer is rendered LOOM_PAD_X/LOOM_PAD_Y larger than the frame so
    the drift never uncovers a bare edge (no seam). Cached in fx_cache/
    like the full-frame fog."""
    if os.path.isfile(path):
        return
    print("Rendering looming fog layer (one-time) ...")
    layer_w, layer_h = OUT_W + LOOM_PAD_X, OUT_H + LOOM_PAD_Y
    small_w, small_h = layer_w // 16, layer_h // 16
    mask = ("nullsrc=s=%dx%d:r=%d:d=%d,format=gray,"
            "geq=lum='255*pow(min(max((Y-0.45*H)/(0.55*H)\\,0)\\,1)\\,1.3)'"
            % (layer_w, layer_h, OUT_FPS, ATMOS_LOOP))
    run_ffmpeg(["ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i",
                "nullsrc=s=%dx%d:r=%d:d=%d" % (small_w, small_h,
                                              OUT_FPS, ATMOS_LOOP),
                "-f", "lavfi", "-i", mask,
                "-filter_complex",
                "[0:v]noise=alls=48:allf=t,tblend=all_mode=average,"
                "eq=brightness=0.65:saturation=0,"
                "scale=%d:%d,boxblur=luma_radius=12:luma_power=2,"
                "format=rgba[fog];"
                "[1:v]format=gray[alpha];"
                "[fog][alpha]alphamerge,format=argb[vout]"
                % (layer_w, layer_h),
                "-map", "[vout]",
                "-frames:v", str(ATMOS_LOOP * OUT_FPS),
                "-c:v", "qtrle", path], "assemble")


def _build_ember_sprite(path):
    """Pre-render a soft glowing ember dot sprite (cached).

    Note: this FFmpeg build ignores geq's alpha plane, so the soft
    edge is built with alphamerge instead of a geq alpha expression."""
    if os.path.isfile(path):
        return
    print("Rendering ember sprite (one-time) ...")
    mask = ("nullsrc=s=64x64:d=1,"
            "geq=lum='255*exp(-(pow(X-32,2)+pow(Y-32,2))/120)',"
            "format=gray")
    run_ffmpeg(["ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=0xff9623:s=64x64:d=1",
                "-f", "lavfi", "-i", mask,
                "-filter_complex",
                "[0:v]format=rgba[fg];[1:v]format=gray[al];[fg][al]alphamerge",
                "-frames:v", "1", path], "assemble")


def _build_ember_loop(path, ember_sprite):
    """Pre-render all ember motion into ONE sparse transparent loop.

    The final assembly then needs a single plain overlay for every ember
    instead of 7 per-frame animated overlays -- this is the step that
    makes 1080p assembly fast. Same seed/params, so it looks identical."""
    if os.path.isfile(path):
        return
    print("Rendering ember loop (one-time) ...")
    params = _ember_params()
    inputs = []
    for _ in params:
        inputs += ["-framerate", str(OUT_FPS), "-loop", "1",
                   "-t", str(ATMOS_LOOP), "-i", ember_sprite]
    fc = ("nullsrc=s=%dx%d:r=%d:d=%d,format=rgba[bg];"
          % (OUT_W, OUT_H, OUT_FPS, ATMOS_LOOP))
    prev = "bg"
    for i, p in enumerate(params):
        fc += ("[%d:v]scale=%d:%d,format=rgba,"
               "colorchannelmixer=aa=%.2f[e%d];"
               % (i, p["size"], p["size"], EMBER_ALPHA, i))
        nxt = "m%d" % i
        fc += ("[%s][e%d]overlay=x='%d+20*sin(t/5+%d)':"
               "y='H+40-mod(t*%d+%d\\,H+200)'[%s];"
               % (prev, i, p["x"], p["phase"], p["speed"], p["offset"], nxt))
        prev = nxt
    fc += "[%s]format=argb[vout]" % prev
    run_ffmpeg(["ffmpeg", "-y", "-v", "error"] + inputs +
                ["-filter_complex", fc,
                 "-map", "[vout]", "-frames:v", str(ATMOS_LOOP * OUT_FPS),
                 "-c:v", "qtrle", path], "assemble")


def _build_atmosphere(path, fog, ember_loop):
    """Pre-render ONE transparent atmosphere loop: bottom-weighted smoky
    fog drift + embers.

    (Looming fog removed 2026-09-20 per user -- it looked off; the smog
    effect carries the atmosphere with its own Speed 4 / Atmosphere 0.25,
    rising like light smoke from a soft source, so it stays visible.)

    The final assembly then needs a single plain overlay per frame
    instead of two full-frame software blends -- much faster, and the
    The fog drifts on an ultra-natural three-harmonic path -- one slow
    45s sweep plus two gentler gust harmonics (two and three cycles per
    loop, so the loop stays seamless). The combined motion wanders like
    real wind: long slow sweeps, easing at the turns, never a mechanical
    back-and-forth. Drift amplitudes stay inside the padding (margins
    >=15px), so no bare edge ever shows."""
    if os.path.isfile(path):
        return
    print("Rendering atmosphere loop (one-time) ...")
    fc = ("[0:v]format=rgba,colorchannelmixer=aa=%.4f[fog];"
          "[1:v]format=rgba[e];"
          "nullsrc=s=%dx%d:r=%d:d=%d,format=rgba[bg];"
          "[bg][fog]overlay=x='-70+32*sin(0.140*t+0.5)+14*sin(0.279*t+2.1)"
          "+6*sin(0.419*t+4.0)':"
          "y='-40+15*cos(0.140*t+1.2)+7*sin(0.279*t+0.3)"
          "+3*cos(0.419*t+2.5)'[v1];"
          "[v1][e]overlay,format=rgba[vout]"
          % (FOG_ALPHA, OUT_W, OUT_H, OUT_FPS, ATMOS_LOOP))
    run_ffmpeg(["ffmpeg", "-y", "-v", "error",
                "-stream_loop", "-1", "-i", fog,
                "-stream_loop", "-1", "-i", ember_loop,
                "-filter_complex", fc,
                "-map", "[vout]",
                "-frames:v", str(ATMOS_LOOP * OUT_FPS),
                "-c:v", "qtrle", path], "assemble")


def _build_slideshow(imgs, per_scene, workdir):
    """Each screenshot becomes a clip with a slow eased zoom
    (Zoom Lens: Speed 18 = brisker, Range 23 = 1.00 -> 1.23 sweep).

    Fast path: frames are rendered with OpenCV (center crop of
    W/z x H/z, resized back to WxH -- ~5ms/frame) and piped raw to the
    encoder. The FFmpeg scale filter re-inits its scaler on EVERY frame
    when eval=frame changes the size, which is what made 1080p assembly
    take an hour; this is ~10x faster with identical framing and the
    same smoothstep easing. Falls back to the FFmpeg filter version if
    OpenCV is unavailable."""
    try:
        import cv2
    except ImportError:
        print("OpenCV not found, using slower FFmpeg zoom ...")
        return _build_slideshow_ffmpeg(imgs, per_scene, workdir)
    clips = []
    for i, img in enumerate(imgs):
        clip = os.path.join(workdir, "zclip_%03d.mp4" % i)
        if os.path.isfile(clip) and os.path.getsize(clip) > 0:
            print("  zoom clip %d/%d ... cached" % (i + 1, len(imgs)))
            clips.append(clip)
            continue
        print("  zoom clip %d/%d ..." % (i + 1, len(imgs)))
        base = cv2.imread(img, cv2.IMREAD_COLOR)
        if base is None:
            fail("assemble", "Could not read image: %s" % img)
        if (base.shape[1], base.shape[0]) != (OUT_W, OUT_H):
            base = cv2.resize(base, (OUT_W, OUT_H),
                              interpolation=cv2.INTER_AREA)
        frames = max(1, round(per_scene * OUT_FPS))
        zin = (i % 2 == 0)
        cmd = (["ffmpeg", "-y", "-v", "error",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", "%dx%d" % (OUT_W, OUT_H),
                "-r", str(OUT_FPS), "-i", "-",
                "-frames:v", str(frames)] + _venc(ENC_QUALITY) +
               ["-pix_fmt", "yuv420p", clip])
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            for n in range(frames):
                t = n / max(1, frames - 1)
                ts = min(1.0, t / ZOOM_SWEEP)  # Speed 18: sweep done early, then hold
                e = ts * ts * (3 - 2 * ts)  # smoothstep: slow at start/end
                z = (1.0 + ZOOM_RANGE * e) if zin else \
                    (1.0 + ZOOM_RANGE * (1 - e))
                cw = max(1, int(round(OUT_W / z)))
                ch = max(1, int(round(OUT_H / z)))
                x = (OUT_W - cw) // 2
                y = (OUT_H - ch) // 2
                frame = cv2.resize(base[y:y + ch, x:x + cw],
                                   (OUT_W, OUT_H),
                                   interpolation=cv2.INTER_LINEAR)
                proc.stdin.write(frame.tobytes())
        except BrokenPipeError:
            proc.wait()
            fail("assemble", "zoom clip encode failed: %s" % clip)
        proc.stdin.close()
        if proc.wait() != 0:
            fail("assemble", "zoom clip encode failed: %s" % clip)
        clips.append(clip)
    lst = os.path.join(workdir, "clips.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '%s'\n" % os.path.abspath(c).replace("'", "'\\''"))
    slideshow = os.path.join(workdir, "slideshow.mp4")
    run_ffmpeg(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", lst, "-c", "copy", slideshow], "assemble")
    return slideshow


def _build_slideshow_ffmpeg(imgs, per_scene, workdir):
    """Fallback zoom builder (no OpenCV): the original FFmpeg
    scale-eval=frame version. Same look, much slower at 1080p.

    Done as scale-up + center crop: FFmpeg's crop filter evaluates w/h
    only once at init, so the zoom is driven by scale with eval=frame
    instead. We always crop from a LARGER image, so there are no
    frame-edge glitches."""
    clips = []
    for i, img in enumerate(imgs):
        clip = os.path.join(workdir, "zclip_%03d.mp4" % i)
        if os.path.isfile(clip) and os.path.getsize(clip) > 0:
            print("  zoom clip %d/%d ... cached" % (i + 1, len(imgs)))
            clips.append(clip)
            continue
        print("  zoom clip %d/%d ..." % (i + 1, len(imgs)))
        frames = max(1, round(per_scene * OUT_FPS))
        zin = (i % 2 == 0)
        # smoothstep easing on frame n (0..frames): slow at start/end.
        # Speed 18: the sweep finishes ZOOM_SWEEP into the scene, then holds.
        e = ("(3*pow(min(1,n/%d/%.6f),2)-2*pow(min(1,n/%d/%.6f),3))"
             % (frames, ZOOM_SWEEP, frames, ZOOM_SWEEP))
        if zin:   # 1.00 -> 1.23 : push in
            z = "1+%.6f*(%s)" % (ZOOM_RANGE, e)
        else:     # 1.23 -> 1.00 : pull out
            z = "%.6f-%.6f*(%s)" % (1 + ZOOM_RANGE, ZOOM_RANGE, e)
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase,"
              "scale=w='iw*(%s)':h='ih*(%s)':eval=frame,"
              "crop=%d:%d:(in_w-out_w)/2:(in_h-out_h)/2,"
              "setsar=1,fps=%d"
              % (OUT_W, OUT_H, z, z, OUT_W, OUT_H, OUT_FPS))
        # -loop 1 turns the still image into a frame stream so the scale
        # animates per frame; -frames:v caps the clip length.
        run_ffmpeg(["ffmpeg", "-y", "-v", "error",
                    "-loop", "1", "-framerate", str(OUT_FPS), "-i", img,
                    "-vf", vf, "-frames:v", str(frames),
                    *_venc(ENC_QUALITY),
                    "-pix_fmt", "yuv420p", clip], "assemble")
        clips.append(clip)
    lst = os.path.join(workdir, "clips.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '%s'\n" % os.path.abspath(c).replace("'", "'\\''"))
    slideshow = os.path.join(workdir, "slideshow.mp4")
    run_ffmpeg(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", lst, "-c", "copy", slideshow], "assemble")
    return slideshow


def _assemble_onepass(imgs, per_scene, use_montage, m_dur, voice_dur, audio,
                      atmos, total):
    """Single-pass final assembly: OpenCV renders the zoomed slideshow
    frames straight into the final encoder through a raw pipe, while
    FFmpeg overlays the pre-baked atmosphere loop, prepends the montage
    hook and muxes the finished audio. The video is encoded ONCE -- the
    previous path encoded every frame twice (per-scene clips, then a
    full re-encode with effects) -- so this is roughly 2x faster with
    identical framing, zoom easing (smoothstep 1.00<->1.23) and effects."""
    import cv2

    # input layout: [montage?], raw slideshow pipe, atmosphere, audio
    # (n_inputs counts input FILES; each "-i file" is 2 list items)
    inputs, n_inputs = [], 0
    if use_montage:
        inputs += ["-i", F_MONTAGE]
        n_inputs += 1
    pipe_i = n_inputs
    inputs += ["-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", "%dx%d" % (OUT_W, OUT_H),
               "-r", str(OUT_FPS), "-i", "-"]
    n_inputs += 1
    atmos_i = n_inputs
    inputs += ["-stream_loop", "-1", "-i", atmos]
    n_inputs += 1
    a_idx = n_inputs
    inputs += ["-i", audio]

    if use_montage:
        fc = ("[0:v]scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,fps=%d,settb=AVTB[v0];"
              "[%d:v]fps=%d,settb=AVTB[v1];"
              "[v0][v1]concat=n=2:v=1:a=0[base];"
              % (OUT_W, OUT_H, OUT_W, OUT_H, OUT_FPS, pipe_i, OUT_FPS))
    else:
        fc = ("[%d:v]fps=%d,settb=AVTB[base];" % (pipe_i, OUT_FPS))
    # User rule 2026-09-20: NO effects during the hook montage -- the fog +
    # ember atmosphere starts exactly where the screenshots begin
    # (montage end = m_dur) and runs to the end of the video.
    fx_start = m_dur if use_montage else 0.0
    fc += ("[base]format=rgba[b];"
           "[%d:v]format=rgba[a];"
           "[b][a]overlay=enable='gte(t,%.3f)'[v1];"
           "[v1]colorbalance=rs=0.05:gs=0.02:bs=-0.04:enable='gte(t,%.3f)',"
           "format=yuv420p[vout]" % (atmos_i, fx_start, fx_start))

    cmd = (["ffmpeg", "-y", "-v", "error"] + inputs +
           ["-filter_complex", fc,
            "-map", "[vout]", "-map", "%d:a" % a_idx,
            "-t", "%.3f" % total] +
           _venc(ENC_QUALITY) +
           ["-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", F_FINAL])
    print("Assembling video with fog + ember effects (single pass) ...")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        for i, img in enumerate(imgs):
            base = cv2.imread(img, cv2.IMREAD_COLOR)
            if base is None:
                proc.kill()
                fail("assemble", "Could not read image: %s" % img)
            if (base.shape[1], base.shape[0]) != (OUT_W, OUT_H):
                base = cv2.resize(base, (OUT_W, OUT_H),
                                  interpolation=cv2.INTER_AREA)
            frames = max(1, round(per_scene * OUT_FPS))
            zin = (i % 2 == 0)
            for n in range(frames):
                t = n / max(1, frames - 1)
                ts = min(1.0, t / ZOOM_SWEEP)  # Speed 18: sweep done early, then hold
                e = ts * ts * (3 - 2 * ts)  # smoothstep: slow at start/end
                z = (1.0 + ZOOM_RANGE * e) if zin else \
                    (1.0 + ZOOM_RANGE * (1 - e))
                cw = max(1, int(round(OUT_W / z)))
                ch = max(1, int(round(OUT_H / z)))
                x = (OUT_W - cw) // 2
                y = (OUT_H - ch) // 2
                frame = cv2.resize(base[y:y + ch, x:x + cw],
                                   (OUT_W, OUT_H),
                                   interpolation=cv2.INTER_LINEAR)
                proc.stdin.write(frame.tobytes())
            print("  scene %d/%d ..." % (i + 1, len(imgs)))
    except BrokenPipeError:
        proc.wait()
        fail("assemble", "final encode failed (encoder died).")
    proc.stdin.close()
    if proc.wait() != 0:
        fail("assemble", "final encode failed.")


def _assemble_twopass(imgs, per_scene, use_montage, m_dur, voice_dur, audio,
                      atmos, total):
    """Previous assembly path (kept as fallback when OpenCV is missing):
    per-scene zoom clips, concat, then a full re-encode with the
    atmosphere overlay. Same look as the single pass, ~2x slower."""
    n = len(imgs)
    print("Building zoom clips for %d scenes ..." % n)
    slideshow = _build_slideshow(imgs, per_scene, F_BUILD_DIR)

    # full filter graph (input layout depends on montage presence)
    # Fog drift + ember motion are pre-baked into atmosphere.mov, so
    # the long final pass evaluates ONE plain overlay per frame.
    inputs = []
    if use_montage:
        inputs += ["-i", F_MONTAGE]
    inputs += ["-i", slideshow,
               "-stream_loop", "-1", "-i", atmos]
    inputs += ["-i", audio]
    # index map: [montage?], slideshow, atmosphere loop, audio
    base_i = 1 if use_montage else 0
    atmos_i = base_i + 1
    a_idx = atmos_i + 1

    if use_montage:
        fc = ("[0:v]scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,fps=%d,settb=AVTB[v0];"
              "[1:v]scale=%d:%d,fps=%d,settb=AVTB[v1];"
              "[v0][v1]concat=n=2:v=1:a=0[base];"
              % (OUT_W, OUT_H, OUT_W, OUT_H, OUT_FPS, OUT_W, OUT_H, OUT_FPS))
    else:
        fc = ("[%d:v]scale=%d:%d,fps=%d,settb=AVTB[base];"
              % (base_i, OUT_W, OUT_H, OUT_FPS))
    # User rule 2026-09-20: NO effects during the hook montage -- the fog +
    # ember atmosphere starts exactly where the screenshots begin
    # (montage end = m_dur) and runs to the end of the video.
    fx_start = m_dur if use_montage else 0.0
    fc += ("[base]format=rgba[b];"
           "[%d:v]format=rgba[a];"
           "[b][a]overlay=enable='gte(t,%.3f)'[v1];"
           % (atmos_i, fx_start))

    fc += ("[v1]colorbalance=rs=0.05:gs=0.02:bs=-0.04:enable='gte(t,%.3f)',"
           "format=yuv420p[vout]" % (fx_start,))

    cmd = (["ffmpeg", "-y", "-v", "error"] + inputs +
           ["-filter_complex", fc,
            "-map", "[vout]", "-map", "%d:a" % a_idx,
            "-t", "%.3f" % total,
            *_venc(ENC_QUALITY),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", F_FINAL])
    print("Assembling video with fog + ember effects ...")
    run_ffmpeg(cmd, "assemble")


def stage_assemble(job, no_fx=False, no_qsv=False):
    """Build final.mp4: hook montage + zoom slideshow + voiceover,
    with fog + ember atmosphere baked in. No captions, ever."""
    global _USE_QSV
    if not shutil.which("ffmpeg"):
        fail("assemble", "ffmpeg not found.")
    _USE_QSV = _qsv_available() and not no_qsv
    print("Encoder: %s" % ("Intel Quick Sync (hardware)" if _USE_QSV
                           else "libx264 (software)"))
    if not os.path.isfile(F_VOICE):
        fail("assemble", "Missing file: %s" % F_VOICE)
    imgs = _find_scene_files()
    if not imgs:
        fail("assemble", "No screenshots found in %s/." % F_SCENES_DIR)

    os.makedirs(F_BUILD_DIR, exist_ok=True)
    voice_dur = ffprobe_duration(F_VOICE)
    n = len(imgs)
    per_scene = voice_dur / n
    print("Scenes: %d | voiceover: %.1fs (%.1fs per scene)"
          % (n, voice_dur, per_scene))

    # 1) slideshow (with zoom) timed to the voiceover
    if no_fx:
        slideshow = os.path.join(F_BUILD_DIR, "slideshow.mp4")
        ext = os.path.splitext(imgs[0])[1]  # .png or .jpg
        pattern = os.path.join(F_SCENES_DIR, "scene_%%03d%s" % ext)
        run_ffmpeg(["ffmpeg", "-y", "-v", "error",
                    "-framerate", str(n / voice_dur), "-i", pattern,
                    *_venc(ENC_QUALITY), "-r", str(OUT_FPS),
                    "-pix_fmt", "yuv420p",
                    "-t", "%.3f" % voice_dur, slideshow], "assemble")
    else:
        # FX path: _assemble_onepass renders the zoomed frames straight
        # into the final encoder, and _assemble_twopass builds its own
        # clips internally -- pre-building clips here rendered every
        # frame twice (the ~9 min assembly waste), so it is skipped.
        slideshow = None

    # 2) audio = montage audio (or silence) + voiceover
    use_montage = os.path.isfile(F_MONTAGE)
    m_dur = ffprobe_duration(F_MONTAGE) if use_montage else 0.0
    if use_montage:
        print("Montage hook: %s (%.1fs)" % (F_MONTAGE, m_dur))
    audio = os.path.join(F_BUILD_DIR, "audio.m4a")
    if use_montage:
        first_audio = F_MONTAGE
        if not ffprobe_has_audio(F_MONTAGE):
            first_audio = os.path.join(F_BUILD_DIR, "silence.m4a")
            run_ffmpeg(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", "anullsrc=r=44100:cl=stereo",
                        "-t", "%.3f" % m_dur, "-c:a", "aac", first_audio],
                       "assemble")
        run_ffmpeg(["ffmpeg", "-y", "-v", "error",
                    "-i", first_audio, "-i", F_VOICE,
                    "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                    "-map", "[a]", "-c:a", "aac", audio], "assemble")
    else:
        audio = F_VOICE

    total = m_dur + voice_dur

    if no_fx:
        # clean assembly, no effects, no captions
        inputs = []
        if use_montage:
            inputs += ["-i", F_MONTAGE]
        inputs += ["-i", slideshow, "-i", audio]
        vf = ("[0:v]scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,fps=%d,settb=AVTB[v0];"
              "[1:v]scale=%d:%d,fps=%d,settb=AVTB[v1];"
              "[v0][v1]concat=n=2:v=1:a=0,format=yuv420p[v]"
              % (OUT_W, OUT_H, OUT_W, OUT_H, OUT_FPS, OUT_W, OUT_H, OUT_FPS)) \
            if use_montage else \
            ("[0:v]scale=%d:%d,fps=%d,format=yuv420p[v]"
             % (OUT_W, OUT_H, OUT_FPS))
        cmd = (["ffmpeg", "-y", "-v", "error"] + inputs +
               ["-filter_complex", vf, "-map", "[v]",
                "-map", "%d:a" % (len(inputs) // 2 - 1),
                "-t", "%.3f" % total,
                *_venc(ENC_QUALITY),
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                F_FINAL])
        print("Assembling clean video ...")
        run_ffmpeg(cmd, "assemble")
    else:
        # 3) fog + ember assets (built once, kept in fx_cache/ forever)
        os.makedirs(FX_CACHE_DIR, exist_ok=True)
        fog = os.path.join(FX_CACHE_DIR, "fog7.mov")
        ember = os.path.join(FX_CACHE_DIR, "ember.png")
        ember_loop = os.path.join(FX_CACHE_DIR, "embers.mov")
        atmos = os.path.join(FX_CACHE_DIR, "atmosphere17.mov")
        _build_fog(fog)
        _build_ember_sprite(ember)
        _build_ember_loop(ember_loop, ember)
        _build_atmosphere(atmos, fog, ember_loop)

        try:
            import cv2  # noqa: F401
            have_cv2 = True
        except ImportError:
            have_cv2 = False
        if have_cv2:
            # Single-pass assembly: the proven recipe. A 4-way chunked
            # parallel attempt measured only ~9% faster on 4 OCPU (21.4
            # vs 23.5 min) with a ~2s tail trim, so it was reverted.
            # Quality first, always.
            _assemble_onepass(imgs, per_scene, use_montage, m_dur,
                              voice_dur, audio, atmos, total)
        else:
            print("OpenCV not found, using slower two-pass assembly ...")
            _assemble_twopass(imgs, per_scene, use_montage, m_dur,
                              voice_dur, audio, atmos, total)

    total_out = ffprobe_duration(F_FINAL)
    print("Done -> %s (%.1fs, %dx%d). Ready to upload."
          % (F_FINAL, total_out, OUT_W, OUT_H))
    need(F_FINAL, "assemble")


# ---------------------------------------------------------------- Stage 9: SEO
_SEO_MARKERS = ["SEO_TITLE", "SEO_DESCRIPTION", "SEO_KEYWORDS",
                "SEO_HASHTAGS", "SEO_PINNED"]


def _seo_complete(text):
    """True only if every required section is present with open+close tags."""
    if not text:
        return False
    return all(("[%s]" % m) in text and ("[/%s]" % m) in text
               for m in _SEO_MARKERS)


def _seo_video_tag(text):
    """The raw title a saved seo_package.txt was generated for (or None)."""
    m = re.search(r"^# VIDEO:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else None


def _parse_title_parts(job):
    """Split the promo title into drama/episode/fragman/hook parts.

    full: 'Daha 17 | 17. Bölüm 2. Fragman ''Seni Seviyorum Kardeim'''
    raw : 'Daha 17'  (text before the first '|')
    -> drama_full='Daha 17', drama='Daha', ep='17', frag='2',
       hook='Seni Seviyorum Kardeim'. Missing parts become ''.
    """
    full = job.full_title or ""
    raw = (job.raw_title or "").strip()
    drama_full = raw
    m = re.search(r"(\d+)\s*\.\s*Bölüm", full, re.IGNORECASE)
    ep = m.group(1) if m else ""
    m = re.search(r"(\d+)\s*\.\s*Fragman", full, re.IGNORECASE)
    frag = m.group(1) if m else ""
    hook = ""
    for pat in (r"''(.+?)''", r"'([^']+)'", r"\"([^\"]+)\"", "“(.+?)”"):
        m = re.search(pat, full)
        if m and m.group(1).strip():
            hook = m.group(1).strip()
            break
    drama = drama_full
    # Strip episode/fragman info already present in the raw title, so the
    # builder never doubles it ("Haysiyet 3. Bölüm 2. Fragman" -> "Haysiyet").
    drama = re.sub(r"\s*\d+\s*\.\s*Bölüm.*$", "", drama,
                   flags=re.IGNORECASE).strip() or drama
    drama = re.sub(r"\s*\d+\s*\.\s*Fragman.*$", "", drama,
                   flags=re.IGNORECASE).strip() or drama
    # Strip a trailing episode number ("Daha 17" -> "Daha" when ep == "17").
    if ep and re.search(r"\s%s$" % re.escape(ep), drama):
        drama = re.sub(r"\s%s$" % re.escape(ep), "", drama).strip() or drama
    return drama_full, drama, ep, frag, hook


def _fit(s, n):
    """Single-line string shortened to n chars at a word boundary."""
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip()
    return cut if cut else s[:n]


def _pick(cands, n, limit):
    """First n candidates that fit within `limit` chars untouched.

    Never cuts a phrase in half; word-boundary truncation is only a last
    resort if n distinct titles cannot be filled otherwise."""
    out = []
    for c in cands:
        c = " ".join(c.split())
        if c and len(c) <= limit and c not in out:
            out.append(c)
        if len(out) == n:
            break
    if len(out) < n:
        for c in cands:
            t = _fit(c, limit)
            if t and t not in out:
                out.append(t)
            if len(out) == n:
                break
    return out


def _htag(s):
    """'#'+letters/digits -- one valid hashtag token."""
    return "#" + re.sub(r"[^\w]", "", s, flags=re.UNICODE)


def _build_seo_package(job):
    """Create the complete SEO package for THIS video, deterministically,
    from its own title parts. No AI service is involved, so this step is
    instant and can never fail or get stuck. Implements the master SEO
    prompt's structure: 5 titles + best title, 2-paragraph description,
    at most 27 keywords, exactly 10 hashtags, pinned comment, thumbnail texts,
    shorts package, scores -- plus the machine-readable marker fields the
    uploader parses."""
    drama_full, drama, ep, frag, hook = _parse_title_parts(job)
    ep_bolum = ("%s. Bölüm" % ep) if ep else ""
    if frag:
        frag_txt = "%s. Fragman" % frag
    elif "fragman" in (job.full_title or "").lower():
        frag_txt = "Fragman"
    else:
        frag_txt = ""
    core = " ".join(x for x in [drama, ep_bolum, frag_txt] if x)

    # ---- 1. titles (max 60 chars, never cut mid-phrase) ----
    cands = []
    if core and hook:
        cands.append("%s: %s!" % (core, hook))
    if core:
        cands.append("%s: Gözyaşlarına Boğulacaksın!" % core)
    if drama and ep and hook:
        cands.append("%s: %s. Bölümde %s!" % (drama, ep, hook))
    if core:
        cands.append("%s: Şok Eden Gelişme!" % core)
    cands.append("%s: Yeni Bölümde Neler Olacak?" % drama_full)
    if hook:
        cands.append("%s Fragmanı: %s!" % (drama_full, hook))
    # compact fallbacks for very long drama names
    if hook:
        cands.append("%s: %s!" % (drama, hook))
    if ep_bolum:
        cands.append("%s %s: Şok Gelişme!" % (drama, ep_bolum))
    cands.append("%s: Yeni Bölüm!" % drama)
    cands.append("%s Yeni Fragman Yayında!" % drama_full)
    titles = _pick(cands, 5, 60)
    best = titles[0]
    why = ("En güçlü başlık budur çünkü dizi adı, bölüm ve fragman bilgisiyle "
           "izleyicinin aradığı duygusal kancayı tek cümlede birleştirir.")

    # ---- 2. description (two paragraphs) ----
    p1 = []
    if ep and frag:
        p1.append("%s dizisinin %s. bölüm %s. fragmanı yayınlandı!"
                  % (drama, ep, frag))
    elif ep:
        p1.append("%s dizisinin %s. bölüm fragmanı yayınlandı!"
                  % (drama, ep))
    else:
        p1.append("%s dizisinin yeni fragmanı yayınlandı!" % drama)
    if hook:
        p1.append("\"%s\" sözleriyle başlayan tanıtım, yeni bölümde "
                  "tansiyonun zirveye çıkacağını gösteriyor." % hook)
    else:
        p1.append("Tanıtım, yeni bölümde tansiyonun zirveye çıkacağını "
                  "gösteriyor.")
    if ep:
        p1.append("%s. bölümde sırlar açığa çıkıyor ve hiçbir şey eskisi "
                  "gibi olmuyor." % ep)
    p1.append("Fragmandaki her sahne izleyiciyi yeni bölüme kilitliyor.")
    desc1 = " ".join(p1)
    desc2 = ("%s ile ilgili tüm fragman analizleri, bölüm özetleri ve en "
             "güncel dizi haberleri için DiziVerse'e abone olmayı unutmayın! "
             "Videoyu beğenmeyi, düşüncelerinizi yorumlarda paylaşmayı ve "
             "bildirimleri açmayı unutmayın. Yeni fragmanlar yayınlanır "
             "yayınlanmaz burada olacak!" % drama)

    # ---- 3. keywords (max 27 -- YouTube rejects 29+ as invalidTags;
    # comma separated, no '#') ----
    specific = [drama_full]
    if ep:
        specific.append("%s %s. bölüm" % (drama, ep))
    if ep and frag:
        specific.append("%s %s. bölüm %s. fragman" % (drama, ep, frag))
    specific += ["%s fragman" % drama, "%s yeni bölüm" % drama,
                 "%s son bölüm" % drama, "%s izle" % drama]
    if ep:
        specific.append("%s %s bölüm fragmanı" % (drama, ep))
    if hook:
        specific.append(hook.lower())
    specific += ["%s oyuncular" % drama, "%s konusu" % drama]
    generics = ["türk dizileri", "türk dizisi", "yeni dizi",
                "dizi fragmanları", "fragman izle", "yeni bölüm fragmanı",
                "2026 dizileri", "dizi özet", "bölüm analizi",
                "dizi haberleri", "drama türkiye", "dizi kesit",
                "duygusal dizi", "aile dizisi", "türk drama", "yeni fragman",
                "viral dizi", "dizi replikleri", "ağlatan sahne",
                "dizi", "fragman", "bölüm", "yeni bölüm", "son bölüm",
                "dizi izle", "türkçe", "dram dizisi", "duygusal anlar",
                "fragmanlar"]
    # shortest generics first: even a very long drama name still fits 25+
    generics.sort(key=lambda k: len(k.encode("utf-8")))
    # very long drama-specific keywords are deferred: they eat the byte
    # budget without adding reach; they are only used if count < 25.
    core = [k for k in specific if len(k.encode("utf-8")) <= 40]
    giants = [k for k in specific if len(k.encode("utf-8")) > 40]
    seen, uniq = set(), []
    for k in core + generics:
        kl = k.strip().lower()
        if kl and kl not in seen:
            seen.add(kl)
            uniq.append(k.strip())
    tags, total = [], 0
    for k in uniq:  # YouTube tag byte budget (API counts UTF-8 bytes)
        b = len(k.encode("utf-8")) + 1
        if total + b > 495 or len(tags) >= 27:
            break
        tags.append(k)
        total += b
    if len(tags) < 25:  # pathological long title: spend budget on giants
        for k in giants:
            kl = k.strip().lower()
            if kl in seen:
                continue
            b = len(k.encode("utf-8")) + 1
            if total + b > 495 or len(tags) >= 27:
                break
            tags.append(k.strip())
            total += b

    # ---- 4. hashtags (exactly 10) ----
    hts = [_htag(drama_full), "#Fragman", "#YeniBölüm", "#TürkDizileri",
           "#DiziVerse"]
    if hook:
        hts.append(_htag(hook))
    hts += ["#DiziFragmanı", "#TürkDizisi", "#2026Dizileri"]
    if ep:
        hts.append(_htag("%s%sBölüm" % (drama, ep)))
    hts += ["#DiziHaberleri", "#BölümÖzeti", "#DiziKeyfi"]
    seen, hashtags = set(), []
    for h in hts:
        hl = h.lower()
        if len(h) > 1 and hl not in seen:
            seen.add(hl)
            hashtags.append(h)
        if len(hashtags) == 10:
            break

    # ---- 5. pinned comment ----
    pin = []
    if hook:
        pin.append("%s!" % hook)
    pin.append("Bu fragmandaki en dokunaklı an sizce hangisiydi?")
    if ep:
        pin.append("%s. bölümde sizleri neler bekliyor?" % ep)
    pin.append("Düşüncelerinizi yorumlara yazın, birlikte tartışalım!")
    pin.append("%s fragman analizlerini kaçırmamak için DiziVerse'e abone "
               "olmayı ve bildirimleri açmayı unutmayın!" % drama)
    pinned = " ".join(pin)

    # ---- 6. thumbnail texts ----
    thumbs = []
    if hook:
        thumbs.append(" ".join(hook.split()[:5]) + "!")
    thumbs += ["Gözyaşları Sel Oldu!", "Şok Gelişme!"]
    seen, thumbs3 = set(), []
    for t in thumbs:
        tl = t.lower()
        if 2 <= len(t.split()) <= 5 and tl not in seen:
            seen.add(tl)
            thumbs3.append(t)
        if len(thumbs3) == 3:
            break

    # ---- 7. shorts package ----
    scands = []
    if hook:
        scands.append("%s: %s!" % (drama, hook))
    if ep_bolum:
        scands.append("%s %s: Şok An!" % (drama, ep_bolum))
    scands.append("Ağlatan Sahne! %s" % drama_full)
    scands.append("%s: Kaçırma!" % drama)
    stitles = _pick(scands, 3, 45)
    sp1 = ("Bu sahne herkesi ağlattı! " + (("\"%s\" " % hook) if hook else "")
           + "%s yeni bölümden kaçırmayın." % drama_full)
    sp2 = "Daha fazla fragman ve dizi içeriği için DiziVerse'e abone ol!"
    skw = (tags[:12] + ["%s shorts" % drama_full, "kısa dizi sahnesi",
                        "duygusal sahne", "dizi shorts", "viral dizi",
                        "türk drama shorts", "dizi kesit", "ağlatan sahne"])
    seen, skeywords = set(), []
    for k in skw:
        kl = k.lower()
        if kl not in seen:
            seen.add(kl)
            skeywords.append(k)
        if len(skeywords) == 20:
            break
    shts = [_htag(drama_full), "#Shorts", "#Fragman", "#TürkDizisi",
            "#DiziShorts", "#DiziVerse", "#Viral", "#KısaVideo",
            "#DuygusalAn"]
    seen, shashtags = set(), []
    for h in shts:
        hl = h.lower()
        if len(h) > 1 and hl not in seen:
            seen.add(hl)
            shashtags.append(h)
        if len(shashtags) == 8:
            break

    # ---- assemble the human-readable package + machine markers ----
    L = []
    L.append("### 1. LONG VIDEO PACKAGE\n")
    for i, t in enumerate(titles, 1):
        L.append("%d. %s" % (i, t))
    L.append("\nEN İYİ BAŞLIK: %s\n%s" % (best, why))
    L.append("\n" + "━" * 30 + "\n\n### 2. SEO DESCRIPTION (LONG VIDEO)\n\n"
             + desc1 + "\n\n" + desc2)
    L.append("\n" + "━" * 30 + "\n\n### 3. KEYWORDS\n\n"
             + ", ".join(tags))
    L.append("\n" + "━" * 30 + "\n\n### 4. HASHTAGS\n\n"
             + " ".join(hashtags))
    L.append("\n" + "━" * 30 + "\n\n### 5. PINNED COMMENT\n\n" + pinned)
    L.append("\n" + "━" * 30 + "\n\n### 6. THUMBNAIL TEXT\n")
    for i, t in enumerate(thumbs3, 1):
        L.append("%d. %s" % (i, t))
    L.append("\n" + "━" * 30 + "\n\n### 7. SHORTS VIDEO PACKAGE\n\n"
             "**Viral Shorts Titles:**")
    for i, t in enumerate(stitles, 1):
        L.append("%d. %s" % (i, t))
    L.append("\n**Shorts Description:**\n\n" + sp1 + "\n\n" + sp2)
    L.append("\n**Shorts Keywords:**\n\n" + ", ".join(skeywords))
    L.append("\n**Shorts Hashtags:**\n\n" + " ".join(shashtags))
    L.append("\n" + "━" * 30 + "\n\n### 8. SEO SCORE\n\n"
             "CTR Skoru: 9/10 -- Başlık dizi adı, bölüm, fragman ve duygusal "
             "kancayı tek cümlede birleştirir.\n"
             "SEO Skoru: 9/10 -- Anahtar kelimeler ve hashtagler aranan "
             "terimlerle örtüşür.\n"
             "Viral Potansiyel: 8/10 -- Duygusal kanca paylaşılabilirliği "
             "yüksek bir andır.")
    L.append("\n" + "━" * 30)
    L.append("\n[SEO_TITLE]\n%s\n[/SEO_TITLE]" % best)
    L.append("\n[SEO_DESCRIPTION]\n%s\n\n%s\n[/SEO_DESCRIPTION]"
             % (desc1, desc2))
    L.append("\n[SEO_KEYWORDS]\n%s\n[/SEO_KEYWORDS]" % ", ".join(tags))
    L.append("\n[SEO_HASHTAGS]\n%s\n[/SEO_HASHTAGS]" % " ".join(hashtags))
    L.append("\n[SEO_PINNED]\n%s\n[/SEO_PINNED]" % pinned)
    return "\n".join(L) + "\n"


def _generate_seo_text(job):
    """Build the SEO package for THIS video's raw title, always fresh.

    The old seo_package.txt (if any) is deleted automatically and the
    package is rebuilt deterministically from the video's own title --
    no AI service is involved, so this step is instant and can never
    fail or get stuck. Never reuses another video's SEO, never uploads
    without a complete package. The file is kept afterwards as a record
    (and for manual YouTube Studio uploads)."""
    if os.path.isfile(F_SEO):
        os.remove(F_SEO)  # old package is never reused; always rebuild
        print("Old %s removed, building fresh ..." % F_SEO)
    text = _build_seo_package(job)
    with open(F_SEO, "w", encoding="utf-8") as f:
        f.write("# VIDEO: %s\n" % job.raw_title)
        f.write(text)
    print("SEO package created -> %s" % F_SEO)
    return text


def _parse_seo(text):
    """Extract the marked fields. Keywords become tags untouched except for
    YouTube's technical limits (byte budget, no '#'); the generated wording
    is never rewritten."""
    def grab(name):
        m = re.search(r"\[%s\]\s*(.*?)\s*\[/%s\]" % (name, name),
                      text, re.DOTALL)
        return m.group(1).strip() if m else ""

    title = grab("SEO_TITLE").splitlines()[0].strip() if grab("SEO_TITLE") else ""
    description = grab("SEO_DESCRIPTION")
    keywords = [k.strip() for k in grab("SEO_KEYWORDS").replace("\n", ",").split(",")
                if k.strip()]
    hashtags = grab("SEO_HASHTAGS").split()
    pinned = grab("SEO_PINNED")

    if not title:
        fail("seo", "Could not parse the SEO package (missing [SEO_TITLE]). "
                    "Check %s." % F_SEO)
    # YouTube tags: max YOUTUBE_TAG_BYTES UTF-8 bytes total (API counts
    # bytes, not characters), no "#" symbols, no stray spaces.
    tags, total = [], 0
    for kw in keywords:
        kw = kw.strip().lstrip("#").strip()
        if not kw:
            continue
        kw_bytes = len(kw.encode("utf-8")) + 1
        if total + kw_bytes > YOUTUBE_TAG_BYTES:
            break
        tags.append(kw)
        total += kw_bytes
    longest = max((len(t.encode("utf-8")) for t in tags), default=0)
    print("Best title : %s (%d chars)" % (title, len(title)))
    print("Tags       : %d keywords, %d UTF-8 bytes total, longest %d bytes"
          % (len(tags), total, longest))
    print("Hashtags   : %d" % len(hashtags))
    return title, description, tags, hashtags, pinned


def stage_seo(job, max_tags=None, tags_override=None):
    """Generate (or verify) the SEO package for this video and fill the job."""
    job.check_meta()  # isolation guard: meta must belong to this URL
    if not job.raw_title:
        # upload-only mode: recover the raw title from video_meta.json
        if os.path.isfile(F_META):
            with open(F_META, encoding="utf-8") as f:
                full = json.load(f).get("title", "")
            job.full_title = full
            job.raw_title = full.split("|")[0].strip()
        if not job.raw_title:
            fail("seo", "No raw title available. Run a full build first, "
                        "or pass --title.")
    print("Promo title : %s" % job.full_title)
    print("Raw title   : %s" % job.raw_title)
    seo_text = _generate_seo_text(job)
    title, description, tags, hashtags, pinned = _parse_seo(seo_text)
    if tags_override is not None:
        tags = [t.strip() for t in tags_override.split(",") if t.strip()]
        print("Tag override: sending %d custom tag(s): %s"
              % (len(tags), ", ".join(tags)))
    else:
        limit = max_tags if max_tags is not None else YOUTUBE_MAX_TAGS
        if limit is not None:
            before = len(tags)
            tags = tags[:limit]
            print("Tag limit: sending %d of %d tags" % (len(tags), before))
    job.seo_title = title
    job.seo_description = description
    job.seo_tags = tags
    job.seo_hashtags = hashtags
    job.seo_pinned = pinned


# ---------------------------------------------------------------- Stage 10: YouTube upload
def _get_youtube():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not os.path.isfile(TOKEN_FILE):
        fail("upload", "Not signed in. Run: python diziverse.py --auth")
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, YOUTUBE_SCOPES)
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def do_auth():
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
    if not os.path.isfile(CLIENT_SECRETS):
        fail("auth",
             "client_secrets.json not found.\n"
             "Free one-time setup:\n"
             " 1. https://console.cloud.google.com -> create a project\n"
             " 2. APIs & Services > Library -> enable 'YouTube Data API v3'\n"
             " 3. APIs & Services > OAuth consent screen -> External, app name + email\n"
             " 4. Credentials > Create Credentials > OAuth client ID -> Desktop app\n"
             " 5. Download the JSON and save it here as client_secrets.json")
    if os.path.isfile(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, YOUTUBE_SCOPES)
        if creds.valid:
            print("Already signed in.")
            return
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, YOUTUBE_SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print("Signed in! Token saved -> %s" % TOKEN_FILE)


def _upload_error_detail(e):
    """One-line diagnosis from a failed upload chunk: HTTP status plus
    the server's own error message (e.g. a 400 here would reveal that
    YouTube rejected the metadata, not the network)."""
    try:
        st = e.resp.status if e.resp is not None else "?"
    except Exception:
        st = "?"
    content = ""
    try:
        c = e.content
        if isinstance(c, bytes):
            c = c.decode("utf-8", "replace")
        m = re.search(r'"message"\s*:\s*"([^"]+)"', c or "")
        content = m.group(1) if m else (c or "").strip()[:160]
    except Exception:
        pass
    return " [HTTP %s%s]" % (st, (": %s" % content) if content else "")


def _http_status(e):
    """HTTP status carried by an upload error, or None when the
    connection died before any response arrived."""
    try:
        return e.resp.status if e.resp is not None else None
    except Exception:
        return None


def _retryable_status(st):
    # None = connection dropped mid-upload: always worth retrying.
    # 408/429/5xx = transient server-side: retry. Any other 4xx
    # (400/401/403/404) is deterministic -- retrying cannot help.
    return st is None or st == 408 or st == 429 or (500 <= st <= 599)


def _upload_video(youtube, job, privacy):
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
    # Hashtags live at the end of the description (YouTube has no separate
    # hashtag field; the first 3 show above the title).
    full_desc = job.seo_description.strip()
    if job.seo_hashtags:
        full_desc += "\n\n" + " ".join(job.seo_hashtags[:10])
    body = {
        "snippet": {
            "title": job.seo_title,
            "description": full_desc,
            "tags": job.seo_tags,
            "categoryId": YOUTUBE_CATEGORY_ID,  # Entertainment
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": YOUTUBE_MADE_FOR_KIDS,  # NOT for kids
        },
    }
    media = MediaFileUpload(F_FINAL, chunksize=1 * 1024 * 1024,
                            resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body,
                                  media_body=media)
    print("Uploading %s ..." % F_FINAL)
    resp = None
    retries = 0
    while resp is None:
        try:
            status, resp = req.next_chunk()
            if status:
                print("  %d%%" % int(status.progress() * 100))
                retries = 0  # progress resets the backoff
        except HttpError as e:  # ResumableUploadError subclasses HttpError
            st = _http_status(e)
            detail = _upload_error_detail(e)
            if not _retryable_status(st):
                fail("upload", "YouTube rejected the upload%s. "
                     "This is not a network problem -- retrying cannot "
                     "help." % detail)
            retries += 1
            if retries > UPLOAD_MAX_RETRIES:
                fail("upload", "Upload failed after %d retries%s"
                     % (UPLOAD_MAX_RETRIES, detail))
            wait = min(2 ** retries, 120)
            print("  upload error%s, retry %d/%d in %ds ..."
                  % (detail, retries, UPLOAD_MAX_RETRIES, wait))
            time.sleep(wait)
    job.youtube_video_id = resp["id"]
    job.youtube_watch_url = "https://youtu.be/%s" % job.youtube_video_id
    print("Uploaded! Video ID: %s" % job.youtube_video_id)
    print("Watch: %s" % job.youtube_watch_url)


def _remove_corner_logos(img):
    """FIRST analyze the frame for channel-logo branding, THEN remove it
    -- before any title text is drawn. All FOUR corners are scanned
    (top-left, top-right, bottom-left, bottom-right), because our texts
    sit at a top corner (drama name) and bottom-left (episode info): a
    logo hiding under either would ruin the thumbnail. Only small
    corner branding is removed -- larger title/drama text is never
    touched, even in a corner. Removal is seamless inpainting, so the
    final thumbnail the user gets carries NO logo at all. Returns
    (img, cleaned_count)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        fail("thumbnail", "opencv-python not found. Run: pip install opencv-python")
    h, w = img.shape[:2]
    zw, zh = int(w * THUMB_CORNER_W), int(h * THUMB_CORNER_H)

    def zone_mask(zone, tight, val_thresh=170):
        """Graphic-pixel mask for a corner zone.

        tight=True: only BRIGHT graphic pixels -- whitish (low saturation,
        value above val_thresh) or vivid (bright and saturated, e.g. a
        glossy blue sphere). This catches the usual channel bugs with
        almost no background texture.
        tight=False: additionally any solid graphic differing from the
        local background, plus edges (catches dimmer colored logos)."""
        hsv = cv2.cvtColor(zone, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
        sat, val = hsv[:, :, 1], hsv[:, :, 2]
        whitish = ((sat < 60) & (val > val_thresh)).astype("uint8") * 255
        vivid = ((sat > 80) & (val > 150)).astype("uint8") * 255
        mask = cv2.bitwise_or(whitish, vivid)
        if not tight:
            bg = np.median(zone.reshape(-1, 3), axis=0)
            dist = np.linalg.norm(zone.astype(np.float32) - bg, axis=2)
            distinct = (dist > 60).astype("uint8") * 255
            edges = cv2.Canny(gray, 30, 90)
            mask = cv2.bitwise_or(mask, distinct)
            mask = cv2.bitwise_or(mask, edges)
        return cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)

    def _rect_gap(a, b):
        """Edge distance between two (x, y, w, h) rects; 0 when touching."""
        ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
        bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
        return max(max(bx0 - ax1, ax0 - bx1, 0),
                   max(by0 - ay1, ay0 - by1, 0))

    def _cluster(boxes):
        """Merge nearby boxes into one logo candidate, starting from the
        largest box. Distant background texture never joins the cluster."""
        seed = max(range(len(boxes)), key=lambda i: boxes[i][2] * boxes[i][3])
        cl = [boxes[seed]]
        rest = [b for i, b in enumerate(boxes) if i != seed]
        changed = True
        while changed:
            changed = False
            x0 = min(b[0] for b in cl)
            y0 = min(b[1] for b in cl)
            x1 = max(b[0] + b[2] for b in cl)
            y1 = max(b[1] + b[3] for b in cl)
            cur = (x0, y0, x1 - x0, y1 - y0)
            for b in rest[:]:
                if _rect_gap(cur, b) <= LOGO_CLUSTER_GAP:
                    cl.append(b)
                    rest.remove(b)
                    changed = True
        x0 = min(b[0] for b in cl)
        y0 = min(b[1] for b in cl)
        x1 = max(b[0] + b[2] for b in cl)
        y1 = max(b[1] + b[3] for b in cl)
        return (x0, y0, x1 - x0, y1 - y0)

    def _touches_title_row(zone, box):
        """True if the candidate logo box touches a large text row -- i.e.
        it is a letter of the drama title (e.g. the first letter of a
        top-left title), never a logo. Only WIDE rows count: a logo's own
        tiny text must not protect itself."""
        zh_, zw_ = zone.shape[:2]
        bx, by, bw, bh = box
        for r in _text_rows(zone, 0.0, min_blob=18):
            if not r["strict"]:
                continue
            if (r["x1"] - r["x0"]) < zw_ * 0.45:
                continue
            if not (bx > r["x1"] + 10 or r["x0"] > bx + bw + 10 or
                    by > r["y1"] + 10 or r["y0"] > by + bh + 10):
                return True
        return False

    def _fits_logo_box(bx, by, bw, bh):
        """Size gates: logos are SMALL. Anything bigger is probably
        drama/title text and must be kept exactly."""
        return not (bw > w * LOGO_MAX_W or bh > h * LOGO_MAX_H
                    or bw * bh > w * h * LOGO_MAX_AREA)

    def logo_bbox(zone, hside, vside):
        """Bounding box of the corner-logo candidate, or None.

        Two passes: the tight (whitish) mask first -- it isolates the usual
        white/silver channel bugs without pulling in background texture;
        the full mask second for colored logos. A box counts when it sits
        in the outer half of the corner zone and in its edge half (top
        half for top corners, bottom half for bottom corners). Nearby
        graphic pieces are clustered (logo + its text), but if the merged
        cluster grows too big (logo glued to background texture) the
        largest fitting single box is tried instead of giving up."""
        zh_, zw_ = zone.shape[:2]
        for tight in (True, False):
            mask = zone_mask(zone, tight)
            cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
            boxes = []
            for c in cnts:
                x, y, bw, bh = cv2.boundingRect(c)
                if bw * bh < 25:  # ignore specks
                    continue
                in_outer = (x <= zw_ / 2) if hside == "left" \
                    else (x + bw >= zw_ / 2)
                in_edge = (y <= zh_ / 2) if vside == "top" \
                    else (y + bh >= zh_ / 2)
                if in_outer and in_edge:
                    boxes.append((x, y, bw, bh))
            if not boxes:
                continue
            merged = _cluster(boxes)
            if _fits_logo_box(*merged):
                return (merged, mask)
            for b in sorted(boxes, key=lambda b: b[2] * b[3],
                            reverse=True):
                if _fits_logo_box(*b):
                    return (b, mask)
        return None

    cleaned = 0
    for vside, hside in (("top", "left"), ("top", "right"),
                         ("bottom", "left"), ("bottom", "right")):
        zx = 0 if hside == "left" else w - zw
        zy = 0 if vside == "top" else h - zh
        zone = img[zy:zy + zh, zx:zx + zw]
        found = logo_bbox(zone, hside, vside)
        tag = "%s-%s" % (vside, hside)
        if found is None:
            print("%s corner: nothing logo-like." % tag)
            continue
        (bx, by, bw, bh), zmask = found
        # Never touch the drama title: a candidate touching a large text
        # row is a title letter, not a logo.
        if _touches_title_row(zone, (bx, by, bw, bh)):
            print("%s: touches drama-title text -- keeping unchanged."
                  % tag)
            continue
        # Density check: the blob must actually look like a graphic,
        # not background noise. (Size gates already passed inside
        # logo_bbox.)
        density = (zmask[by:by + bh, bx:bx + bw] > 0).mean()
        if density < LOGO_MIN_DENSITY:
            print("%s: faint corner texture, not a logo -- keeping." % tag)
            continue
        # Expand slightly so text+graphic combos are removed entirely,
        # then inpaint for a seamless reconstruction.
        pad = 8
        x0 = max(0, zx + bx - pad)
        y0 = max(0, zy + by - pad)
        x1 = min(w, zx + bx + bw + pad)
        y1 = min(h, zy + by + bh + pad)
        patch = img[y0:y1, x0:x1].copy()
        # Inpaint mask: bright logo pixels (whitish or vivid), with a lower
        # value threshold so the logo's soft glow/halo is covered too,
        # closed to fill interior holes (e.g. inside a swoosh curve), then
        # grown a little so anti-aliased edges and outlines are fully
        # covered. A full-background mask would repaint the whole patch as
        # one flat block, so it is deliberately NOT used here.
        pmask = zone_mask(patch, tight=True, val_thresh=130)
        pmask = cv2.morphologyEx(pmask, cv2.MORPH_CLOSE,
                                 np.ones((15, 15), np.uint8))
        pmask = cv2.dilate(pmask, np.ones((5, 5), np.uint8), iterations=1)
        inpainted = cv2.inpaint(patch, pmask, 4, cv2.INPAINT_TELEA)
        # feathered blend: only the logo pixels are replaced, fading out
        # softly so no rectangular seam is visible
        feather = cv2.GaussianBlur(pmask.astype(np.float32) / 255.0,
                                   (15, 15), 0)[..., None]
        img[y0:y1, x0:x1] = (inpainted.astype(np.float32) * feather +
                             patch.astype(np.float32) * (1 - feather)
                             ).astype(np.uint8)
        print("Removed logo from %s corner." % tag)
        cleaned += 1

    return img, cleaned


def _download_promo_thumbnail(job):
    """Download the promo's own thumbnail. Returns the local file path."""
    if not shutil.which("yt-dlp"):
        fail("thumbnail", "yt-dlp not found.")
    print("Downloading promo thumbnail ...")
    subprocess.run(_ytdlp_cmd("--skip-download",
                    "--write-thumbnail", "-o", "thumb_raw.%(ext)s",
                    job.url, cookie_file=getattr(job, "cookie_file", None)),
                   capture_output=True, text=True)
    cands = [f for f in os.listdir(".")
             if f.startswith(F_THUMB_RAW + ".") and not f.endswith(".json")]
    if not cands:
        fail("thumbnail", "Could not download the promo thumbnail.")
    return cands[0]


def _clean_thumbnail(job, manual_thumb=None):
    """Clean corner logo(s) from the promo's OWN thumbnail.

    Kept for the --thumb manual override and as a fallback when no
    screenshots exist. The normal path now builds the thumbnail from
    the most suspenseful screenshot (see _build_suspense_thumbnail)."""
    try:
        import cv2
    except ImportError:
        fail("thumbnail", "opencv-python not found. Run: pip install opencv-python")
    src = manual_thumb or _download_promo_thumbnail(job)
    img = cv2.imread(src)
    if img is None:
        fail("thumbnail", "Could not read thumbnail: %s" % src)
    img, _ = _remove_corner_logos(img)
    cv2.imwrite(F_THUMB_CLEAN, img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    print("Thumbnail -> %s" % F_THUMB_CLEAN)
    return F_THUMB_CLEAN


def _text_rows(img_bgr, y0_frac=0.45, min_blob=30):
    """Text rows in the lower part of a screenshot.

    img_bgr is the COLOR frame (colored banner text like an orange "yeni
    bolum" is detected too -- a grayscale input would hide it). Returns a
    list of dicts, one per horizontal text line found below y0_frac of
    the frame: x0/y0/x1/y1 (full-frame coords), cy (row center y), area
    (letter-blob area), n (blob count) and strict (True when the row
    passes the strict real-text-line checks: >=3 similar-height blobs
    with regular horizontal spacing). Free, OpenCV only. Shared by the
    banner penalty (selection) and the banner covering (thumbnail
    build), so both see the same rows. min_blob is the smallest
    letter-blob side kept (the penalty uses 30 -- big banners; the
    covering uses 18 so small "yeni bolum"-style lines are seen too)."""
    import cv2
    import numpy as np
    h, w = img_bgr.shape[:2]
    y_base = int(h * y0_frac)
    roi = img_bgr[y_base:, :]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    s = hsv[:, :, 1].astype(int)
    v = hsv[:, :, 2].astype(int)
    masks = [((s < 90) & (v > 195)).astype(np.uint8) * 255,    # white text
             (v < 70).astype(np.uint8) * 255,                   # black text
             ((s > 100) & (v > 120)).astype(np.uint8) * 255]    # colored text
    blobs = []  # (cx, cy, bw, bh)
    for m in masks:
        mc = cv2.morphologyEx(m, cv2.MORPH_CLOSE,
                              np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(mc, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, bw, bh = cv2.boundingRect(c)
            if min_blob < bw < 300 and min_blob < bh < 170 \
                    and 0.2 < bw / max(1, bh) < 8.0:
                blobs.append((x + bw / 2.0, y + bh / 2.0, bw, bh))
    # group into horizontal rows
    rows, used = [], [False] * len(blobs)
    order = sorted(range(len(blobs)), key=lambda i: blobs[i][1])
    for i in order:
        if used[i]:
            continue
        cy, bh = blobs[i][1], blobs[i][3]
        band = max(20.0, bh * 0.6)
        row = [j for j in order
               if not used[j] and abs(blobs[j][1] - cy) <= band]
        for j in row:
            used[j] = True
        rows.append(row)
    out = []
    for row in rows:
        if len(row) < 2:
            continue
        hs = sorted(blobs[j][3] for j in row)
        med = hs[len(hs) // 2]
        line = [j for j in row if 0.5 * med <= blobs[j][3] <= 2.0 * med]
        if len(line) < 2:
            continue
        # regular horizontal spacing => a real text line. Word spaces
        # ("GUL VE DIKEN") are larger than letter gaps, so the biggest
        # gap is allowed to be a word space and only the rest must be
        # regular -- otherwise multi-word titles never count as strict.
        xs = sorted(blobs[j][0] for j in line)
        gaps = [xs[k + 1] - xs[k] for k in range(len(xs) - 1)]
        mg = sum(gaps) / len(gaps)
        strict = False
        if mg > 0 and len(line) >= 3:
            core = sorted(gaps)[:-1] if len(gaps) >= 4 else gaps
            mcg = sum(core) / len(core)
            var = sum((g - mcg) ** 2 for g in core) / len(core)
            # tight gate: real text has very regular letter spacing;
            # background texture (leaves, fences, railings) does not.
            # A loose gate once faked "text found" and shipped a
            # textless thumbnail -- never again.
            strict = (var ** 0.5) / mcg <= 0.5
        x0 = min(blobs[j][0] - blobs[j][2] / 2.0 for j in line)
        x1 = max(blobs[j][0] + blobs[j][2] / 2.0 for j in line)
        y0 = min(blobs[j][1] - blobs[j][3] / 2.0 for j in line) + y_base
        y1 = max(blobs[j][1] + blobs[j][3] / 2.0 for j in line) + y_base
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "cy": (y0 + y1) / 2.0,
                    "area": sum(blobs[j][2] * blobs[j][3] for j in line),
                    "n": len(line), "strict": strict})
    return out


def _banner_text_penalty(img_bgr):
    """Penalty 0..1 for a big burned-in text banner ("3. BOLUM /
    2. FRAGMAN", "YENI SEZON ...", ...) in the lower half of a 720p
    screenshot. The user never wants such text in the thumbnail.

    Drama titles sit at the TOP of the frame, so only the lower 55% is
    inspected -- they are never penalized. Row detection is shared with
    the thumbnail builder via _text_rows(). Takes the color frame so
    colored banner text is counted too."""
    h, w = img_bgr.shape[:2]
    y_base = int(h * 0.45)
    rows = _text_rows(img_bgr, 0.45)
    strict = [r for r in rows if r["strict"]]
    if sum(r["n"] for r in strict) < 3:
        return 0.0
    tarea = sum(r["area"] for r in strict)
    return min(1.0, tarea / float((h - y_base) * w))


def _top_title_score(img_bgr):
    """Score 0..1 for big drama-title text at the TOP of a 720p screenshot
    ("SEVDAN BIR ATES", "CIRKIN", ... as in the user's samples). The user
    wants the drama name at top-left/right/middle of the thumbnail.

    Method (free, OpenCV only): strict text rows (regular letter spacing,
    via _text_rows) in the top 30%. A row spanning nearly the full width
    is background texture (chandelier/arch), not a title -- rejected.
    Score is the rows' blob area over the top region. Takes the color
    frame (colored title text must be seen)."""
    import cv2
    import numpy as np
    h, w = img_bgr.shape[:2]
    rows = [r for r in _text_rows(img_bgr, 0.0, min_blob=30)
            if r["strict"] and r["y1"] <= h * 0.30
            and (r["x1"] - r["x0"]) <= w * 0.85
            and (r["x1"] - r["x0"]) >= w * 0.12]
    if not rows:
        return 0.0
    area = sum(r["area"] for r in rows)
    return min(1.0, area / float(w * h * 0.30))


def _bottom_text_score(img_bgr):
    """How much real text sits in the bottom 30% of the frame -- the
    user's samples always KEEP the 'N. BOLUM / N. FRAGMAN / TANITIM'
    line at the bottom, so frames showing it are preferred for the
    thumbnail. Takes the color frame (colored text must be seen)."""
    H, W = img_bgr.shape[:2]
    rows = [r for r in _text_rows(img_bgr, 0.70, min_blob=18)
            if r["strict"] and (r["x1"] - r["x0"]) >= W * 0.12]
    if not rows:
        return 0.0
    area = sum(r["area"] for r in rows)
    return min(1.0, area / float(W * H * 0.30))


def _thumb_font(size, weight=800):
    """Montserrat (user's sample jaisa font) with latin-ext for Turkish
    characters; DejaVu-Bold fallback. Free (OFL)."""
    try:
        from PIL import ImageFont
    except ImportError:
        fail("thumbnail", "Pillow not found. Run: pip install pillow")
    name = "montserrat-v31-latin_latin-ext-%d.ttf" % weight
    for p in (os.path.join(os.getcwd(), "fonts", name),
              os.path.join(os.path.dirname(os.getcwd()), "fonts", name)):
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    fp = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if os.path.isfile(fp):
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _text_size(font, text):
    """(width, height) of rendered text."""
    l, tp, r, b = font.getbbox(text)
    return max(1, r - l), max(1, b - tp)


def _block_hits_face(tx, ty, tw, th, faces, pad=24):
    """True if a text block overlaps any detected face box."""
    for (fx, fy, fw, fh) in faces:
        if (tx - pad < fx + fw and tx + tw + pad > fx and
                ty - pad < fy + fh and ty + th + pad > fy):
            return True
    return False


def _draw_show_texts(img_bgr, job):
    """Draw the sample-style thumbnail texts (the user's two new
    samples + the older approved ones):
    - drama name (auto-detected from the video title) at a TOP corner:
      top-left, or top-right when a face sits on the left
    - episode info HUGE at bottom-left: "{ep}. BÖLÜM" and, below it,
      smaller "{frag}. FRAGMAN" / "{n}. ÖN İZLEME"
    White bold Montserrat with the same subtle dim blurred shade as
    before (barely visible, never a solid box, never a logo). Text
    sizes shrink when a face sits behind them, per the user's rule.
    Returns the BGR image."""
    import cv2
    import numpy as np
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        fail("thumbnail", "Pillow not found. Run: pip install pillow")
    H, W = img_bgr.shape[:2]
    faces = _face_boxes(img_bgr)

    full = job.full_title or ""
    drama_full, drama, ep, frag, hook = _parse_title_parts(job)
    drama = (drama or drama_full or "").strip()
    sub_kind, sub_num = "FRAGMAN", frag
    m = re.search(r"(\d+)\s*\.\s*Ön İzleme", full, re.IGNORECASE)
    if m:
        sub_kind, sub_num = "ÖN İZLEME", m.group(1)
    elif not frag:
        m = re.search(r"(\d+)\s*\.\s*Tanıtım", full, re.IGNORECASE)
        if m:
            sub_kind, sub_num = "TANITIM", m.group(1)
    ep_line = ("%s. BÖLÜM" % ep) if ep else drama
    sub_line = ("%s. %s" % (sub_num, sub_kind)) if sub_num else sub_kind

    def shade(base, text, font, tx, ty):
        """The user's subtle dim shade: blurred dark text copy at low
        opacity -- barely visible, never a solid box."""
        tw, th = _text_size(font, text)
        pad = 28
        sh = Image.new("RGBA", (tw + pad * 2, th + pad * 2),
                       (0, 0, 0, 0))
        ImageDraw.Draw(sh).text((pad, pad), text, font=font,
                                fill=(0, 0, 0, 70))
        sh = sh.filter(ImageFilter.GaussianBlur(6))
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        layer.paste(sh, (tx - pad, ty - pad), sh)
        return Image.alpha_composite(base, layer)

    base = Image.fromarray(cv2.cvtColor(img_bgr,
                                        cv2.COLOR_BGR2RGB)).convert("RGBA")

    # -- drama name: top-left, or top-right when a face sits left --
    if drama:
        size = 58
        font = _thumb_font(size, 700)
        tw, th = _text_size(font, drama)
        tx, ty = 60, 30
        if _block_hits_face(tx, ty, tw, th, faces):
            tx = W - 60 - tw
        if _block_hits_face(tx, ty, tw, th, faces) and size > 44:
            size = 44
            font = _thumb_font(size, 700)
            tw, th = _text_size(font, drama)
            tx = 60
        base = shade(base, drama, font, tx, ty)
        pil = base.convert("RGB")
        ImageDraw.Draw(pil).text((tx, ty), drama, font=font,
                                 fill=(255, 255, 255))
        base = pil.convert("RGBA")

    # -- episode block: bottom-left, HUGE (user's explicit rule) --
    ep_size, sub_size = 118, 62
    for _ in range(6):
        ep_font = _thumb_font(ep_size, 800)
        sub_font = _thumb_font(sub_size, 700)
        ew, eh = _text_size(ep_font, ep_line)
        sw_, sh_ = _text_size(sub_font, sub_line)
        bx, gap = 56, 14
        bw = max(ew, sw_)
        bh = eh + gap + sh_
        by = H - 56 - bh
        if not _block_hits_face(bx, by, bw, bh, faces):
            break
        ep_size = int(ep_size * 0.88)
        sub_size = int(sub_size * 0.88)
    else:
        ep_font = _thumb_font(ep_size, 800)
        sub_font = _thumb_font(sub_size, 700)
        ew, eh = _text_size(ep_font, ep_line)
        sw_, sh_ = _text_size(sub_font, sub_line)
        bx, gap = 56, 14
        by = H - 56 - (eh + gap + sh_)
    base = shade(base, ep_line, ep_font, bx, by)
    base = shade(base, sub_line, sub_font, bx, by + eh + gap)
    pil = base.convert("RGB")
    dr = ImageDraw.Draw(pil)
    dr.text((bx, by), ep_line, font=ep_font, fill=(255, 255, 255))
    dr.text((bx, by + eh + gap), sub_line, font=sub_font,
            fill=(255, 255, 255))
    print("Thumbnail text: top '%s' | bottom '%s' / '%s'" %
          (drama, ep_line, sub_line))
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)




def _face_boxes(img_bgr):
    """Face bounding boxes [(x, y, w, h)] in full-res coords. YuNet DNN
    is tried first (the server's opencv has no Haar objdetect at all);
    Haar cascade is the fallback where it exists. All free."""
    import cv2
    h, w = img_bgr.shape[:2]
    try:
        if hasattr(cv2, "FaceDetectorYN"):
            for mp in (os.path.join(os.getcwd(),
                                    "face_detection_yunet_2023mar.onnx"),
                       os.path.join(os.path.dirname(os.getcwd()),
                                    "face_detection_yunet_2023mar.onnx")):
                if os.path.isfile(mp):
                    det = cv2.FaceDetectorYN.create(mp, "", (320, 320),
                                                    0.6, 0.3, 5000)
                    det.setInputSize((w, h))
                    _, faces = det.detect(img_bgr)
                    if faces is not None:
                        return [(int(f[0]), int(f[1]),
                                 int(f[2]), int(f[3])) for f in faces]
                    return []
    except Exception:
        pass
    try:
        cp = os.path.join(cv2.data.haarcascades,
                          "haarcascade_frontalface_default.xml")
        if os.path.isfile(cp) and hasattr(cv2, "CascadeClassifier"):
            cc = cv2.CascadeClassifier(cp)
            if not cc.empty():
                gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
                boxes = cc.detectMultiScale(gray, 1.1, 4,
                                            minSize=(40, 40))
                return [(int(x), int(y), int(bw), int(bh))
                        for (x, y, bw, bh) in boxes]
    except Exception:
        pass
    return []


def _source_text_rows_full(img_bgr):
    """Strict burnt-in text rows ANYWHERE in the frame (top/middle/bottom).

    The user (2026-09-27) never wants the video's own text in the
    thumbnail -- like the verified channels' clean thumbnails. _text_rows
    with y0_frac=0.0 scans the whole frame; min_blob=18 also catches
    small subtitle lines. Only strict rows (regular letter spacing)
    count, so background texture never fakes a hit."""
    return [r for r in _text_rows(img_bgr, 0.0, min_blob=18)
            if r["strict"]]


def _crop_out_text(img_bgr, min_area_frac=0.30):
    """Zoom-crop a 16:9 frame to cut out burnt-in text bands.

    The user allows zooming in to drop source text, but the thumbnail
    must stay a proper complete composition (verified-channel style):
    the crop keeps 16:9, covers >= min_area_frac of the frame (0.30 --
    a real zoom-in like the verified channels' punchy close-ups, never
    a sliver), stays
    centered on the main face, and the face must remain inside. Only
    top/bottom text bands can be cropped away -- text sitting in the
    middle of the frame cannot be removed by a clean crop. Returns
    (image, cropped_bool); the cropped image is resized back to the
    input size. Free, OpenCV only."""
    import cv2
    H, W = img_bgr.shape[:2]
    rows = _source_text_rows_full(img_bgr)
    if not rows:
        return img_bgr, False
    top_cut, bot_cut = 0.0, float(H)
    for r in rows:
        if r["y1"] <= H * 0.35:
            top_cut = max(top_cut, r["y1"])
        elif r["y0"] >= H * 0.60:
            bot_cut = min(bot_cut, r["y0"])
        else:
            return img_bgr, False  # middle text: no clean 16:9 crop
    if top_cut <= 0 and bot_cut >= H:
        return img_bgr, False
    faces = _face_boxes(img_bgr)
    if faces:
        fx, fy, fw, fh = max(faces, key=lambda b: b[2] * b[3])
        cx = fx + fw / 2.0
    else:
        fx = fy = fw = fh = None
        cx = W / 2.0
    ch = bot_cut - top_cut
    cw = ch * 16.0 / 9.0
    if cw > W:  # safety; cannot happen on 16:9 input
        cw = float(W)
        ch = cw * 9.0 / 16.0
    x0 = min(max(cx - cw / 2.0, 0.0), W - cw)
    y0 = top_cut  # ch <= bot_cut - top_cut, so [y0, y0+ch] is text-free
    if ch <= 0 or cw <= 0:
        return img_bgr, False
    x0i, y0i, cwi, chi = (int(round(x0)), int(round(y0)),
                          int(round(cw)), int(round(ch)))
    if cwi * chi < min_area_frac * W * H:
        return img_bgr, False
    if faces:
        # main face (70% of its box) must stay inside the crop
        if not (x0i <= fx + fw * 0.3 and fx + fw * 0.7 <= x0i + cwi
                and y0i <= fy + fh * 0.3 and fy + fh * 0.7 <= y0i + chi):
            return img_bgr, False
    crop = img_bgr[y0i:y0i + chi, x0i:x0i + cwi]
    if crop.size == 0:
        return img_bgr, False
    return cv2.resize(crop, (W, H),
                      interpolation=cv2.INTER_AREA), True


def _pick_suspense_shot(files):
    """Pick the most suspenseful screenshot -- ALWAYS one WITH A PERSON,
    like the user's samples: a face/emotional close-up that carries the
    drama's suspense. Scores every screenshot by total detected
    person-face area (YuNet DNN -- free ONNX, OpenCV's own model -- or
    Haar where available). Any frame WITH a face outranks every frame
    without one; among people shots the largest face wins, with
    contrast/sharpness breaking ties.

    NO-TEXT RULE (user 2026-09-27): the video's own burnt-in text must
    not appear in the thumbnail -- like the verified channels' clean
    thumbnails. A text-free frame is always preferred over a frame with
    text; a text frame can only win when no clean frame exists, and then
    _crop_out_text zoom-crops the text away. Deterministic, no AI."""
    import cv2
    scored = []  # (file, face_frac, has_text, (contrast, sharp))
    for f in files:
        img = cv2.imread(f)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        area = float(w * h)
        face_frac = 0.0
        for (fx, fy, fw, fh) in _face_boxes(img):
            face_frac += (fw * fh) / area
        has_text = bool(_source_text_rows_full(img))
        contrast = float(gray.std())
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        scored.append((f, face_frac, has_text, (contrast, sharp)))
    if not scored:
        return None
    # A person must show -- ALWAYS: any frame with a face outranks every
    # frame without one. Then the no-text rule: text-free frames form the
    # pool; text frames are the last resort only.
    people = [s for s in scored if s[1] > 0] or scored
    clean = [s for s in people if not s[2]]
    pool = clean or people

    def rank(s):
        _f, face_frac, has_text, q = s
        return (face_frac, q[0], q[1])

    best = max(pool, key=rank)
    _f, face_frac, has_text, _q = best
    print("Suspense shot: %s (people %.0f%% of frame%s)" %
          (os.path.basename(_f), min(100.0, face_frac * 100),
           ", WITH source text - will zoom-crop" if has_text
           else ", no source text"))
    return _f


def _build_suspense_thumbnail(job):
    """Build the thumbnail from the most suspenseful screenshot.

    Uses the UNZOOMED screenshots (full frame), picks the shot with the
    most human presence (a face/emotional close-up -- a person MUST
    show), removes corner logos via inpainting, and draws the
    sample-style texts: the drama name (auto-detected from the title)
    at a top corner, and the episode info HUGE at bottom-left
    ("{ep}. BÖLÜM" + "{frag}. FRAGMAN" / "{n}. ÖN İZLEME") -- exactly
    like the user's approved samples. No logos are ever drawn.
    Returns the thumbnail path."""
    import cv2
    files = _find_fullscene_files()
    if not files:
        print("No screenshots found -- falling back to the promo thumbnail.")
        return _clean_thumbnail(job)
    shot = _pick_suspense_shot(files)
    if not shot:
        print("Could not read screenshots -- falling back to the promo "
              "thumbnail.")
        return _clean_thumbnail(job)
    img = cv2.imread(shot)
    if img.shape[1] != 1280 or img.shape[0] != 720:
        img = cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA)
    # NO-TEXT RULE (user 2026-09-27): last-resort text frame -> zoom in to
    # cut the burnt-in text out (verified-channel style clean thumbnail).
    img, was_cropped = _crop_out_text(img)
    if was_cropped:
        print("Thumbnail: burnt-in source text zoom-cropped out.")
    img, n_logos = _remove_corner_logos(img)
    print("Logo analysis: %d logo(s) removed -- final thumbnail has no "
          "logo." % n_logos)
    img = _draw_show_texts(img, job)
    cv2.imwrite(F_THUMB_CLEAN, img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    print("Thumbnail -> %s" % F_THUMB_CLEAN)
    return F_THUMB_CLEAN




def _set_thumbnail(youtube, job, thumb_path):
    from googleapiclient.http import MediaFileUpload
    youtube.thumbnails().set(
        videoId=job.youtube_video_id,
        media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg")
    ).execute()
    print("Thumbnail set.")


def stage_upload(job, privacy=YOUTUBE_PRIVACY, manual_thumb=None,
                 skip_thumb=False):
    """Upload final.mp4 with the job's SEO, clean + set the thumbnail."""
    if not os.path.isfile(F_FINAL):
        fail("upload", "Video not found: %s" % F_FINAL)
    job.check_meta()  # isolation guard
    youtube = _get_youtube()
    _upload_video(youtube, job, privacy)
    if not skip_thumb:
        if manual_thumb:
            thumb = _clean_thumbnail(job, manual_thumb=manual_thumb)
        elif job.url:
            thumb = _build_suspense_thumbnail(job)
        else:
            print("No promo URL -- skipping thumbnail.")
            thumb = None
        if thumb:
            _set_thumbnail(youtube, job, thumb)
    # Archive the finished file so the user gets a download link for
    # review (stage_clean wipes final.mp4 when the next video starts).
    try:
        os.makedirs("downloads", exist_ok=True)
        arc = os.path.join("downloads", "%s.mp4" % job.youtube_video_id)
        shutil.copyfile(F_FINAL, arc)
        print("Archived for download: %s" % arc)
    except Exception as e:
        print("Download archive skipped: %s" % e)
    print("\nAll done! %s" % job.youtube_watch_url)


# =============================================================================
# PART 6 -- main orchestrator
# =============================================================================
def _parse_args():
    ap = argparse.ArgumentParser(
        description="DiziVerse UNIVERSAL WORKFLOW: one command, link -> uploaded video.")
    ap.add_argument("url", nargs="?",
                    help="YouTube promo URL (full pipeline)")
    ap.add_argument("--auth", action="store_true",
                    help="one-time Google sign-in, then exit")
    ap.add_argument("--skip-upload", action="store_true",
                    help="build final.mp4 but do not upload (review it first)")
    ap.add_argument("--upload-only", action="store_true",
                    help="upload the existing final.mp4 (no rebuild, no clean)")
    ap.add_argument("--thumb-only", action="store_true",
                    help="only clean the promo thumbnail (for manual Studio "
                         "upload), then exit")
    ap.add_argument("--title",
                    help="override: full promo title (raw title = before first |)")
    ap.add_argument("--thumb", help="use this thumbnail instead of auto-cleaning")
    ap.add_argument("--privacy", default=YOUTUBE_PRIVACY,
                    choices=["public", "unlisted", "private"])
    ap.add_argument("--skip-thumb", action="store_true",
                    help="skip thumbnail cleaning/upload")
    ap.add_argument("--max-tags", type=int, default=None,
                    help="diagnostic: send only the first N tags instead of all")
    ap.add_argument("--tags",
                    help="diagnostic: comma-separated custom tag list, "
                         "overrides the SEO file's tags entirely")
    ap.add_argument("--keep-intro", action="store_true",
                    help="do not cut a leading ad/promo from the download")
    ap.add_argument("--keep-script", action="store_true",
                    help="use your own script.txt (it must be 900+ words) "
                         "instead of the free AI; the file is kept, not cleaned")
    ap.add_argument("--no-fx", action="store_true",
                    help="assemble without fog/ember/zoom effects (clean output)")
    ap.add_argument("--no-qsv", action="store_true",
                    help="force software x264 even if Quick Sync is available")
    return ap.parse_args()


def _recover_job_from_disk(url, title_override):
    """Build a VideoJob for --upload-only from the artifacts on disk."""
    job = VideoJob(url or "")
    if os.path.isfile(F_META):
        with open(F_META, encoding="utf-8") as f:
            meta = json.load(f)
        job.url = meta.get("url", "")
        job.video_id = meta.get("video_id", "") or extract_video_id(job.url)
        job.source_file = meta.get("file", "")
        job.full_title = meta.get("title", "")
    if title_override:
        job.full_title = title_override
    if job.full_title:
        job.raw_title = job.full_title.split("|")[0].strip()
    return job


def main():
    args = _parse_args()
    args.url = (args.url or "").strip()  # tolerate pasted whitespace

    if args.auth:
        do_auth()
        return

    # --thumb-only: just produce the thumbnail for a manual
    # YouTube Studio upload (used when the API upload keeps failing).
    if args.thumb_only:
        job = _recover_job_from_disk(args.url, args.title)
        if not job.url:
            fail("thumb-only", "No promo URL found. Run inside the video folder.")
        thumb = _build_suspense_thumbnail(job)
        print("\nThumbnail -> %s" % thumb)
        print("Upload it manually in YouTube Studio: video -> Details -> Thumbnail.")
        return

    # --upload-only: upload the already-built final.mp4 (no clean, no rebuild)
    if args.upload_only:
        if not os.path.isfile(F_FINAL):
            fail("upload-only", "%s not found. Build it first with:\n"
                                "    python diziverse.py \"URL\"" % F_FINAL)
        job = _recover_job_from_disk(args.url, args.title)
        print("Upload-only mode: using the existing %s." % F_FINAL)
        banner(1, 2, "Generating SEO package for this video")
        stage_seo(job, max_tags=args.max_tags, tags_override=args.tags)
        banner(2, 2, "Uploading to YouTube")
        stage_upload(job, privacy=args.privacy, manual_thumb=args.thumb,
                     skip_thumb=args.skip_thumb)
        print("\nRemember: open YouTube Studio and PIN the posted comment.")
        return

    # Full pipeline: one URL -> uploaded video.
    if not args.url:
        fail("args", "Usage: python diziverse.py \"https://www.youtube.com/watch?v=XXXX\"")
    t0 = time.time()
    STAGES = 9 if not args.skip_upload else 8
    job = VideoJob(args.url)
    job.keep_script = args.keep_script
    if args.title:
        job.full_title = args.title
        job.raw_title = args.title.split("|")[0].strip()

    banner(1, STAGES, "Cleaning previous video's artifacts")
    stage_clean(job)

    banner(2, STAGES, "Downloading promo (1080p) + trimming intro ad")
    stage_download(job, keep_intro=args.keep_intro)

    banner(3, STAGES, "Building the sub-18s hook montage")
    stage_montage(job)

    banner(4, STAGES, "Grabbing scene screenshots")
    stage_scenes(job)

    banner(5, STAGES, "Transcribing trailer audio (Turkish)")
    stage_transcribe(job)

    banner(6, STAGES, "Writing the 7-8 min narration script")
    stage_script(job)

    banner(7, STAGES, "Generating the Turkish male voiceover")
    stage_voiceover(job)

    banner(8, STAGES, "Assembling final.mp4 (zoom + fog + looming fog + embers)")
    stage_assemble(job, no_fx=args.no_fx, no_qsv=args.no_qsv)

    if args.skip_upload:
        print("\nDone (build only). Watch %s, then upload with:" % F_FINAL)
        print("  python diziverse.py --upload-only")
    else:
        banner(9, STAGES, "SEO + uploading to YouTube (thumbnail + comment)")
        stage_seo(job, max_tags=args.max_tags, tags_override=args.tags)
        stage_upload(job, privacy=args.privacy, manual_thumb=args.thumb,
                     skip_thumb=args.skip_thumb)

    mins = (time.time() - t0) / 60
    print("\n" + "=" * 60)
    print("ALL DONE in %.1f minutes." % mins)
    if not args.skip_upload:
        print("Remember: open YouTube Studio and PIN the posted comment")
        print("(the YouTube API cannot pin it by itself).")
    print("Next promo? Just run again with the new link:")
    print("  python diziverse.py \"NEW_URL\"")
    print("=" * 60)


if __name__ == "__main__":
    main()
