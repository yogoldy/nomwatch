# Roadmap

## v0.1 — Manual proof of concept (done)
- [x] Camera RTSP enabled via camera-account credentials
- [x] MediaMTX bridge pulling RTSP → HLS, bound to loopback only
- [x] Tailscale serve exposing the stream tailnet-only (funnel confirmed off)
- [x] Manually verified: LAN-unreachable test, reboot survival, cert/hostname handling

## v0.2 — Installable CLI (in progress)
- [ ] `pip install nomwatch` / Homebrew tap
- [ ] `nomwatch setup` wizard: prompts for camera IP, RTSP port, camera-account creds; detects existing Tailscale login; generates MediaMTX config; sets up auto-start service (launchd/systemd)
- [ ] `nomwatch status`: health check across camera reachability, MediaMTX process, Tailscale serve status, cert validity
- [ ] Config stored in a single local file, permissioned 600, never touched by version control

## v0.3 — Detection engine
- [ ] Startup check for a local model server (e.g. `ollama list`-style probe) and prefer it if present
- [ ] Integration path for existing open-source video-analysis tools that can consume our HLS/RTSP output (e.g. Frigate-style detectors) as the preferred detection backend
- [ ] Bundled lightweight fallback model (YOLOv8n-class) for zero-config out-of-box detection
- [ ] Motion-only heuristic as the absolute minimum fallback
- [ ] Event object schema: timestamp, confidence, clip reference, camera id

## v0.4 — Notifications + storage
- [ ] ntfy.sh integration (default, zero-account-needed option)
- [ ] Pushover integration (alternative)
- [ ] Google Drive OAuth flow + upload of short event clips
- [ ] Local event log (simple SQLite or JSON) for history/review independent of Drive

## v0.5 — Polish
- [ ] Simple local web dashboard (served over the same Tailscale tunnel) to review event history/clips without needing Drive
- [ ] Docker Compose option for NAS users
- [ ] Docs: supported camera list, troubleshooting guide, security FAQ

## Stretch goals (not committed)
- [ ] Native iOS companion app with APNs push (replacing ntfy/Pushover)
- [ ] Pluggable storage backends beyond Google Drive (S3-compatible, local-only NAS)
- [ ] Pluggable private-mesh backend beyond Tailscale
- [ ] Per-pet identification (multi-pet households) via the detection layer
- [ ] Community-contributed camera compatibility list

## Explicitly out of scope for the foreseeable future
- Any NomWatch-operated cloud/account/relay service
- Paid tiers of any kind
