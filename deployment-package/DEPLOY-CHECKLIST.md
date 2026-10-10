# DiziVerse Deployment Checklist — New Ampere A1 Server

Target: `VM.Standard.A1.Flex`, 4 OCPU / 24 GB, Ubuntu ARM, me-dubai-1.
Package: `~/workspace/deployment-package/`

## Phase 1 — Server ready

- [ ] 1. Ampere instance `RUNNING`, public IP assigned
- [ ] 2. Exactly ONE instance running (no duplicates — check OCI console/API)
- [ ] 3. Shape verified: 4 OCPU / 24 GB (Always Free — no charge)
- [ ] 4. Run `scripts/setup-server.sh` as `ubuntu` on the server
- [ ] 5. Verify: `python3 --version`, `ffmpeg -version`, `yt-dlp --version`, `cloudflared --version`

## Phase 2 — Deploy channel files

Copy from the package to `~/diziverse-server/` on the server:

- [ ] 6. `ch2/diziverse.py` + `ch2/token.json` → `~/diziverse-server/ch2/`
- [ ] 7. `ch3/diziverse.py` + `ch3/token.json` + `ch3/cookies.txt` → `~/diziverse-server/ch3/`
- [ ] 8. `ch4/diziverse.py` + `ch4/cookies.txt` (+ `voice_reference.mp3`) → `~/diziverse-server/ch4/`
- [ ] 9. `shared/*` → `~/diziverse-server/shared/`
- [ ] 10. Ownership/permissions: `chown ubuntu:ubuntu`, tokens+cookies `chmod 600`
- [ ] 11. Verify sizes + sha256 against `MANIFEST.txt`

## Phase 3 — Tokens & identity

- [ ] 12. ch2 token: call YouTube API `channels.list(mine=true)` → expect ch2 channel
- [ ] 13. ch3 token: same check → expect "Yeni Türk Dizileri"
- [ ] 14. ch4 OAuth: user does Google Allow (see `ch4-oauth-README.md`), then verify channel "Dizi Dünyası Deniz"

## Phase 4 — ch4 voice

- [ ] 15. Run `scripts/clone-ch4-voice.sh` on the server
- [ ] 16. Post-deploy TTS test → owner listens and approves
- [ ] 17. Until approved, ch4 uses edge-tts fallback (do NOT upload with unapproved voice)

## Phase 5 — Services & tunnels

- [ ] 18. Start dashboard + maint API services on the server
- [ ] 19. Start fresh Cloudflare Quick Tunnels (dashboard + maint)
- [ ] 20. Capture new random hostnames; update `dashboard_url.txt` and `live_url.txt`
- [ ] 21. Verify externally via live browser: `/api/status?key=...` returns JSON

## Phase 6 — Production verification (in order)

- [ ] 22. Safe download test on the new IP (production yt-dlp args, one short promo)
- [ ] 23. Real ch2 upload → confirm in `done.txt` + dashboard
- [ ] 24. Real ch3 upload → confirm
- [ ] 25. Real ch4 upload → confirm (only after voice approved)

## Phase 7 — After uploads work

- [ ] 26. Build shared queue/coordinator (ch2 → ch3 → ch4 flow)
- [ ] 27. Restore per-channel voices/effects/thumbnails
- [ ] 28. Re-enable watchers (promo-watch, pending-drain, pipeline-health)
- [ ] 29. Disable the `ampere-a1-retry` cron

## Do NOT

- Never push `~/workspace/your_files/diziverse.py` (stale) over the server copies
- Never use files from `~/workspace/server-backup-2026-10-10/` (truncated)
- Never create paid shapes, extra block volumes, or a second instance
- Never upload ch4 with an unapproved voice
