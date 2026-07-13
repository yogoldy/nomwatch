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
- [x] ~~Still unverified against a real camera: the actual clip build (audio transcode to AAC from real G711 tracks)~~ VERIFIED live (2026-07-12): two real feeding events produced stitched pre-roll clips off actual MediaMTX segments - ffprobe confirms h264 video + AAC audio, 12.6s duration against an expected ~13s window (5s pre-roll + 8s post-confirm)
- [x] Upload retries with backoff (3 attempts) instead of a one-shot attempt; push notification send failures are caught (they used to be able to crash the whole monitoring loop) and recorded on the event
- [x] Clip-window flush fix: the loop now waits one extra segment length after the post-confirm window before stitching, since MediaMTX only finalizes a segment file when it rolls to the next one - previously the tail of the clip window could still be inside an unflushed segment

## v0.5 — Polish
- [ ] Docker Compose option for NAS users
- [ ] Docs: supported camera list, troubleshooting guide, security FAQ
- [ ] Auto-detect Google Drive for Desktop sync folder on Windows/Linux more robustly (currently macOS-tested only)
- [ ] Swap Flask's dev server for a real WSGI server (e.g. `waitress`) for `nomwatch ui` - current dev server is fine for local single-user use but not meant for anything beyond that

## v0.6 — Setup UI (replaces CLI wizard as the primary path)
Full spec: `docs/UI_SPEC.md`. Decision: local web app (not native/Electron),
becomes the primary first-run/reconfigure experience; `nomwatch setup`
(CLI) stays available underneath for power users, automation, and headless
bridge devices - not being retired.
- [x] Screen 1 first slice built (`nomwatch/webui.py`, `nomwatch ui` command): camera config form + local model detection/install, with hover info-icons on every field and a password show/hide toggle. Verified: real Flask app serving real HTML, `/api/check-model` responding correctly against actual Ollama state, tested live in-browser (not just via curl).
- [x] All 6 wizard screens + dashboard shipped and live-tested against real hardware (real Tapo camera, real Ollama, real ntfy pushes, real MediaMTX recording)
- [x] Dashboard heartbeat: the monitoring loop writes `~/.config/nomwatch/heartbeat.json` after every poll (atomic write); the dashboard shows "last check Ns ago + what the model saw" and warns if the loop looks hung - directly answers "is this thing actually working"
- [x] Stale-settings detection: pid files record start time; the dashboard warns when config.yml was saved AFTER MediaMTX/monitoring started (i.e. the process is running with old settings) with a one-click restart - the exact failure mode behind the previous two bug reports
- [x] Clip gallery on the dashboard: per-event thumbnails (ffmpeg-generated, cached), in-browser playback (HTTP range serving), download, and delete - subsumes the earlier "event history/clip review dashboard" idea
- [x] Config backup/restore on the dashboard (download config.yml / upload + validate + regenerate mediamtx.yml)
- [x] Camera troubleshooting: failed "test connection" now runs a TCP probe + parses ffmpeg stderr to distinguish wrong-IP / RTSP-off-or-wrong-port / wrong-credentials / wrong-stream-path, each with concrete next steps (verified live against the real camera in all four failure modes)
- [x] Duplicate-monitor protection: the dashboard detects `nomwatch run` processes started outside the UI (launchd service, stray terminal) and refuses to start a second loop that would double every notification
- [x] Security hardening: mediamtx.yml (contains RTSP credentials) now written 0600; RTSP credentials percent-encoded in all URLs (special chars in passwords used to silently break the stream); MediaMTX/monitoring launched with CWD pinned to the config dir (MediaMTX was dropping auto-generated cert files into whatever directory the UI was started from); user-typed paths sanitized (a quoted paste created a literal `'` directory tree and clips silently went to the wrong place - found live)
- [x] Mobile-responsive dashboard + wizard (viewport meta + responsive layout), for checking on the pet from a phone
- [ ] Real progress indicator during `ollama pull` instead of blocking until done
- [x] Screen 2: pre-roll/post-confirm timing controls with live debounce-math feedback (engine selector, motion-gating, and a real drag-to-draw zone picker added in v0.7)
- [x] Screen 3: storage - built with the SIMPLIFIED model (pre-roll cache
      always local; ONE final clip destination: local / Drive sync / Drive
      API / none). The "local AND Drive" combined destination from
      UI_SPEC.md is not built and would still need the config model change
      noted there
- [x] Screen 4: notifications - plain-language ntfy explainer, App
      Store/Play Store/web app links, generated topic with copy buttons +
      QR code for phone scanning, custom topic override, real test-send.
      (Pushover works in the backend/CLI but has no UI on this screen yet)
- [ ] Screen 5 (optional toggle, off by default): appearance identification
      - ask the local vision model to describe species/color/etc. as an
        enrichment of the feeding-event record, not a new event category.
        (The pet description IS now used by detection.py as of v0.7 - it's
        fed into the vision prompt. The remaining unbuilt part is the
        optional describe-appearance enrichment of the event record.)
- [x] This also subsumes the earlier "event history/clip review" dashboard
      idea (previously listed here as v0.5's "web dashboard") - shipped as
      the dashboard clip gallery above
- [ ] Optional `nomwatch ui --expose` flag to auto-wire the UI over
      `tailscale serve` the same clean way `bridge.py` already does for the
      HLS stream - today exposing the UI remotely requires a manual
      `tailscale serve` command

## v0.7 — Detection reliability (highest priority - see below)
The single-frame "ask a 4B vision model yes/no on one still image" approach
in `detection.py` is not reliable enough on its own: confirmed live
(2026-07-12) - the current setup produced confident FEEDING triggers on an
empty room with nothing in frame. The consecutive-poll debounce only
protects against intermittent noise; it does nothing when the model is
consistently wrong on a given camera angle/lighting condition. This is now
treated as the most important open problem in the project - a false
notification (or worse, a false "upload nothing happened" silently eaten by
debounce) undermines the entire premise of the tool. Decision, made
2026-07-12: pursue all of the following together, not as alternatives -

**Root cause found + fixed (2026-07-12):** the model was answering "is there
food in the bowl?" not "is an animal eating?". Confirmed live by capturing
real frames and reading them: an empty IR scene with a full bowl fired
FEEDING:yes 0.8 on 18/18 frames. Rewriting the prompt to require a *visible
animal with head/mouth at the bowl* (food alone = hard NO), and wiring in
`pet_description`, took that to **0/18 false positives with 6/6 real
cat-eating frames still detected**. All items below then built on top:

- [x] **Prompt fix + `pet_description` wiring** — `build_prompt(pet_description)`
      in `detection.py`; "no visible animal" is now an explicit NO regardless
      of food. Verified 100%→0% empty-scene false positives on the real camera.
- [x] **Motion-gating**: `MotionOnlyDetector` implemented for real (ffmpeg
      frame-diff, no image library). The vision model is only invoked when
      motion vs. the previous frame exceeds `motion_threshold` (default 2.0;
      measured noise floor ~0.3, moving cat ~20-48). Also selectable standalone
      as `detection.engine: "motion"` (no model server needed).
- [x] **Hybrid corroboration mode** (`detection.engine: "hybrid"`): motion AND
      the vision model must both agree for a poll to count. Verified with an
      injected-frame test: a model "yes" on a static frame is suppressed;
      motion-only (walk-past) doesn't fire.
- [x] **Zone cropping, wired for real**: real drag-to-draw bounding-box picker
      on setup screen 2 (over a live camera snapshot); normalized coords saved
      to config and applied via ffmpeg `crop` to BOTH the motion diff and the
      image sent to the model. Verified live in-browser.
- [ ] Optional, not done this pass: a setup-time calibration step - capture N
      frames of the actual empty feeder on THIS camera and run real
      classifications to measure this camera/model's baseline
      false-positive rate, then suggest a `min_confidence` /
      `consecutive_required` that would have suppressed it.

## Client/viewer architecture decision
NomWatch always requires one real host machine (Mac, and eventually
Linux/Windows) running the bridge (MediaMTX + detection + web UI). Phones,
tablets, and other computers are ALWAYS viewers/remote-controls only, never
capable of running the bridge itself - iOS/iPadOS in particular can't run a
persistent background RTSP+ffmpeg+local-model pipeline, and Apple wouldn't
allow that as an App Store app regardless. Decisions:
- [ ] **macOS menu-bar wrapper** - realistic and planned. Since a real Mac
      is already required to run the bridge, wrapping the existing
      Python/Flask tool as a small native menu-bar app (e.g. via `rumps`)
      that starts/stops NomWatch and opens the web UI is a reasonable,
      proportionate step - not a full rewrite.
- **Native iOS/iPadOS app: NOT planned.** Would only ever be a
  viewer/remote (never the bridge itself), and the ongoing cost (Apple
  Developer Program enrollment, App Store review, a second codebase to
  maintain) sits awkwardly against NomWatch's free/no-paid-tiers identity.
  Instead: iOS's "Add to Home Screen" feature lets anyone turn the existing
  web UI into a full-screen, chrome-free home-screen icon with zero extra
  native code - effectively a free pseudo-app wrapper we already get once
  the web UI exists. Revisit only if this decision is deliberately
  reconsidered later, not by default feature creep.

## Stretch goals (not committed)
- [ ] Pluggable storage backends beyond Google Drive (S3-compatible, local-only NAS)
- [ ] Pluggable private-mesh backend beyond Tailscale
- [ ] Per-pet identification (multi-pet households) via the detection layer
- [ ] Community-contributed camera compatibility list
- [ ] Formal host-vs-client/multi-device delegation (today it's implicit -
      whichever machine runs `nomwatch run`/`nomwatch ui` is "the bridge" by
      virtue of running it there; nothing coordinates multiple devices or
      tracks a designated bridge role)

## Explicitly NOT planned (recorded so the idea isn't lost, but not to be implemented without a separate product-scope conversation)
- Generalizing detection beyond "feeding event" to arbitrary configurable
  event types (presence, package delivery, intrusion, etc.). The
  architecture could probably support this, but NomWatch's identity is
  specifically a pet feeder monitor for the foreseeable future - no code
  should branch on an "event type" concept. See `docs/UI_SPEC.md`.
- Native iOS/iPadOS companion app - see "Client/viewer architecture
  decision" above.

## Explicitly out of scope for the foreseeable future
- Any NomWatch-operated cloud/account/relay service
- Paid tiers of any kind
