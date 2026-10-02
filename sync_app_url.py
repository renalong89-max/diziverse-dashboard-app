#!/usr/bin/env python3
"""Sync the dashboard tunnel URL to the app's url.json.

The Android app fetches this JSON on launch to auto-update its backend URL
when the Cloudflare tunnel domain changes. Run after tunnel URL changes,
or from a cron.

Usage:
    python3 sync_app_url.py
    python3 sync_app_url.py --push   # also push to GitHub (needs gh auth)
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_URL_FILE = os.path.expanduser(
    "~/workspace/goals/youtube-channel-management/files/"
    "diziverse-dashboard-app/dashboard_url.txt"
)
APP_URL_JSON = os.path.join(HERE, "url.json")

def get_current_url():
    try:
        with open(DASHBOARD_URL_FILE, "r") as f:
            url = f.read().strip()
            return url if url.startswith("https://") else None
    except Exception:
        return None

def get_json_url():
    try:
        with open(APP_URL_JSON, "r") as f:
            return json.load(f).get("url", "").strip()
    except Exception:
        return None

def main():
    current = get_current_url()
    if not current:
        print("ERROR: dashboard_url.txt se URL nahi mila")
        sys.exit(1)

    existing = get_json_url()
    if existing == current:
        print("URL already up to date: %s" % current[:60])
        return

    with open(APP_URL_JSON, "w") as f:
        json.dump({"url": current}, f)
    print("Updated url.json: %s" % current[:60])

    if "--push" in sys.argv:
        # Push to GitHub (requires gh CLI auth)
        r = subprocess.run(
            ["git", "add", "url.json"],
            cwd=HERE, capture_output=True, text=True
        )
        if r.returncode != 0:
            print("git add failed: %s" % r.stderr[:200])
            sys.exit(1)
        r = subprocess.run(
            ["git", "commit", "-m", "Update dashboard URL"],
            cwd=HERE, capture_output=True, text=True
        )
        # commit may fail if nothing to commit (already handled above)
        r = subprocess.run(
            ["git", "push"],
            cwd=HERE, capture_output=True, text=True
        )
        if r.returncode != 0:
            print("git push failed: %s" % r.stderr[:200])
            sys.exit(1)
        print("Pushed to GitHub.")

if __name__ == "__main__":
    main()
