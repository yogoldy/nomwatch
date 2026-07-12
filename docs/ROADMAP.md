# Roadmap

## v0.1 — Manual proof of concept (done)
- [x] Camera RTSP enabled via camera-account credentials
- [x] MediaMTX bridge pulling RTSP → HLS, bound to loopback only
- [x] Tailscale serve exposing the stream tailnet-only (funnel confirmed off)
- [x] Manually verified: LAN-unreachable test, reboot survival, cert/hostname handling

## v0.2 — Installable CLI (done)
- [x] `nomwatch setup` wizard: prompts for camera IP, RTSP port, camera-account creds; detects existing Tailscale/MediaMTX binaries; generates MediaMTX config
- [x] `nomwatch status` / `nomwatch doctor`: health checks for camera config, MediaMTX/Tailscale/ffmpeg binaries
- [x] Config stored in a single local file (`~/.config/nomwatch/config.yml`), permissioned 600, gitignored
- [x] Auto-start service wiring: `nomwatch setup` offers to install a launchd user agent (macOS) so `nomwatch run` starts at login and restarts automatically if it crashes (`KeepAlive`); `nomwatch service-status` / `nomwatch service-uninstall` manage it. Verified: built the real plist, confirmed launchctl load/list output. Linux/systemd not yet wired (tracked below).
- [x] PyPI-ready packaging: added classifiers/keywords/project URLs to `pyproject.toml`; verified a clean `python3 -m build` produces a valid sdist + wheel, and that the built wheel installs and runs correctly in a brand-new virtualenv with no source tree present. Not yet actually published to PyPI (that's a one-time `twine upload` whenever ready).
- [ ] Homebrew tap
- [ ] systemd --user unit for Linux auto-start (launchd/macOS only for now - see `nomwatch/service.py`)

## v0.3 — Detection engine
- [x] Startup check for a local model server (`ollama list`-style probe via `/api/tags`) and prefer it if present
- [x] Auto-pick a vision-capable local model (e.g. `gemma3:4b`) out of whatever's installed, ignoring text-only models
- [x] `nomwatch detect-test`: captures one live frame via ffmpeg and runs a single classification pass through the local Ollama vision model
- [x] Event object schema: timestamp, confidence, clip reference, camera id, reasoning string
- [x] Continuous polling loop wired into `nomwatch run` (long-running, Ctrl+C to stop)
- [x] Consecutive-detection debounce: user sets seconds of continuous eating required before an event fires (real-world tested: correctly ignored a single ambiguous frame, fired once per continuous feeding streak - see `scripts/simulate_poll.py`)
- [ ] Integration path for existing open-source video-analysis tools that can consume our HLS/RTSP output (e.g. Frigate-style detectors) as an alternative detection backend
- [ ] Bundled lightweight fallback model (YOLOv8n-class) for zero-config out-of-box detection when no local model server is present
- [ ] Motion-only heuristic as the absolute minimum fallback

## v0.4 — Notifications + storage
- [x] ntfy.sh integration (default, zero-account-needed option) - fires the moment feeding is confirmed, doesn't wait for clip/upload
- [x] Pushover integration (alternative)
- [x] Google Drive OAuth flow + upload of event clips (`docs/GOOGLE_DRIVE_SETUP.md` walks through the one-time OAuth client setup each user needs)
- [x] Post-confirmation clip recording (`nomwatch/clip.py`) - starts recording the moment feeding is CONFIRMED, for a user-configurable duration
- [x] Local event log (JSONL at `~/.config/nomwatch/events.jsonl`) including clip path + Drive link per event
- [x] Real pre-roll buffer: MediaMTX continuously records rolling local segments (auto-deleted after a short retention window); on a confirmed event, segments are stitched into a clip spanning `confirm_time - pre_roll_seconds` through `confirm_time + clip_post_confirm_seconds`. Verified against synthetic segments (15.2s clip against an expected ~15s window). No second RTSP connection needed for clips at all when this is enabled, since MediaMTX is already recording continuously.
- [x] Storage options without requiring a Google Cloud project: `local` (just a folder, zero setup) and `google_drive_sync` (copies into the Google Drive for Desktop app's sync folder, reusing whatever account is already signed in - zero OAuth). The original `google_drive_api` OAuth flow is kept as an advanced/headless option, not the default.
- [x] **Bug fixed (found via real live test against actual MediaMTX output, not synthetic segments):** `bridge.py`'s `recordPath` hardcoded a trailing `.mp4`, but MediaMTX already appends its own extension based on `recordFormat` - this produced `....mp4.mp4` filenames that silently broke `clip.py`'s filename regex, making `find_segments_covering()` return empty and pre-roll clip building fail 100% of the time against a real camera (while passing fine against hand-named synthetic test files). Fixed by removing the hardcoded extension; re-verified the regex correctly parses real-shaped filenames and correctly rejects the old buggy double-extension form.
- [ ] Verify clip recording doesn't interfere with concurrent detection polling on cameras with a low concurrent-RTSP-client limit (moot when pre-roll/continuous recording is enabled, since detection and recording no longer need separate connections - still relevant for the pre-roll-off fallback path)
- [ ] Still unverified against a real camera: the actual clip build (audio transcode to AAC from real G711 tracks) once real segments are correctly named - the live test never got past the empty-segments bug, so this specific step needs one more live pass

## v0.5 — Polish
- [ ] Simple local web dashboard (served over the same Tailscale tunnel) to review event history/clips without needing Drive
- [ ] Docker Compose option for NAS users
- [ ] Docs: supported camera list, troubleshooting guide, security FAQ
- [ ] Auto-detect Google Drive for Desktop sync folder on Windows/Linux more robustly (currently macOS-tested only)

## Stretch goals (not committed)
- [ ] Native iOS companion app with APNs push (replacing ntfy/Pushover)
- [ ] Pluggable storage backends beyond Google Drive (S3-compatible, local-only NAS)
- [ ] Pluggable private-mesh backend beyond Tailscale
- [ ] Per-pet identification (multi-pet households) via the detection layer
- [ ] Community-contributed camera compatibility list

## Explicitly out of scope for the foreseeable future
- Any NomWatch-operated cloud/account/relay service
- Paid tiers of any kind
