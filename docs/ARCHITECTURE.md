# Architecture & Security Model

> **Local-first host direction (2026-07-14):**
> [`ADR 0001`](adr/0001-local-first-host-and-private-remote-access.md)
> defines the proposed authenticated LAN baseline, private Tailscale Serve
> integration, local account/data model, and complete host service lifecycle.
> Where it differs from this older overview, ADR 0001 is the current proposal.

## Threat model / design goals

NomWatch's entire reason for existing is that pet feeder cams currently force a choice between "pay a subscription and trust a vendor's cloud with home video" or "no smart features at all." We reject both defaults:

- The camera's video **never** touches the public internet.
- No inbound ports are opened on the home router. No port-forwarding, no UPnP, no DDNS.
- The only network surface exposed beyond the LAN is a private mesh network (Tailscale tailnet) that requires authenticated device membership to reach anything.
- Credentials (camera RTSP creds, cloud storage tokens, notification keys) are stored in local, permission-restricted files — never embedded in shipped code, never logged, never sent to a NomWatch-operated server (there is no NomWatch-operated server).

## Components

### 1. Camera
Any LAN camera with RTSP (ONVIF optional, for future motion-event hooks). No cloud account, no vendor app required at runtime — only used once to enable RTSP and set a local camera-account username/password (this is a one-time setup step in whatever the camera's companion app is, e.g. Tapo app's "Advanced Settings → Camera Account").

### 2. Bridge device
Any always-on machine on the same LAN as the camera: old laptop, Raspberry Pi, Mac mini, NAS with Docker. Runs:

- **MediaMTX** (or go2rtc): pulls the RTSP stream and re-serves it as HLS. Bound to `127.0.0.1` only — never the LAN interface, never `0.0.0.0`. WebRTC output is disabled by default because ICE negotiation can advertise local network interfaces to a client, which is a real leak vector for a "LAN-invisible" design; HLS is plain HTTP/TCP and can't escape a loopback bind.
- **Tailscale**: joins the user's private tailnet. `tailscale serve` publishes the loopback HLS endpoint at `https://<device>.<tailnet>.ts.net` — TLS-terminated automatically by Tailscale's cert issuance, reachable only by devices authenticated into that same tailnet. **`tailscale funnel` is never used** — funnel makes an endpoint public, which is exactly what this project exists to avoid.
- **Detection + notification + upload** (see below) also run here, so raw video never needs to leave this device except as short clips deliberately uploaded to the user's own cloud storage.

### 3. Detection engine (pluggable)
Priority order, decided at startup:

1. **Existing local model server**: NomWatch checks for a locally running model host (e.g. issues an `ollama list`-equivalent request) and, if found, prefers it — avoids bundling/managing yet another inference stack if the user already has one.
2. **Existing open-source video-analysis software** that can consume the same RTSP/HLS stream we've already built (e.g. Frigate, DeepStack-style detectors) — preferred over writing custom computer vision, since these projects already solve object detection, zones, and event debouncing well.
3. **Lightweight local fallback model** (YOLOv8n-class or similar) bundled as a minimum viable default, so NomWatch works out of the box with zero extra setup.
4. **Motion-only heuristic** as the absolute floor, if no model is available/desired — cheap, zero-dependency, better than nothing.

The detection layer's job is narrow: watch the stream, decide "this looks like a feeding event," and emit an event object (timestamp, confidence, short clip reference) to the rest of the pipeline. It is designed as a swappable interface so better engines can be dropped in later without touching the bridge or upload code.

#### v0.7 detection reliability (implemented)

The single-frame "ask a small vision model yes/no" approach was producing constant false positives: the model answered "is there food in the bowl?" instead of "is an animal eating?", firing on an empty scene with a full bowl (confirmed live: 18/18 empty-scene frames → false positive). Three cooperating mechanisms now address this, selectable via `detection.engine`:

- **`motion`** — a non-AI floor. Frame-to-frame motion only; no model server needed. Cannot tell eating from walking past, but never fires on a static scene.
- **`ollama`** — the vision model decides. With `motion_gating` on (default), the model is *not even called* on a frame with no motion since the last one, so a static bowl-with-food never reaches the LLM. This is the most direct fix for the empty-scene false positive.
- **`hybrid`** — motion AND the vision model must both agree for a poll to count. Suppresses both empty-scene model hallucinations (nothing moved) and motion-only noise (a cat walking past without eating).

The prompt itself was also rewritten to require a *visible animal with its head/mouth at the bowl* — "food in the bowl with no animal" is now an explicit hard NO. `detection.pet_description` is fed to the model as a hint. **Zone cropping** (`zone_detection_enabled` + normalized `zone_x/y/w/h`, drawable in the setup UI) crops every frame to just the bowl area before *both* the motion diff and the model call.

#### Why no image library (ffmpeg-only motion + cropping)

Motion detection and zone cropping are implemented with **ffmpeg alone** — no Pillow, numpy, or OpenCV. Motion is a mean-absolute-difference over a 48×48 grayscale thumbnail (ffmpeg decodes the JPEG to a raw gray buffer; the diff is a plain Python loop over ~2,304 bytes). Cropping uses ffmpeg's `crop` filter with `in_w/in_h` expressions, so no pixel dimensions or image library are needed. ffmpeg is *already* a hard dependency (frame capture + clip building), so reusing it keeps the dependency/attack surface tiny — the priority for a privacy-first, zero-bloat local tool. Pillow (simple pixel access) and numpy (fast array math) were considered; both were rejected as unnecessary weight for what is essentially a subtraction on a tiny grid. Revisit if per-region motion or heavier CV is ever needed.

### 4. Notifications
v1 uses a free third-party push service — **ntfy.sh** or **Pushover** — chosen because both require no Apple Developer account, no custom iOS app, and no NomWatch-operated backend. A dedicated iOS app with native APNs push is a listed stretch goal, not a v1 requirement.

### 5. Storage
v1 uploads short event clips (e.g. ~2 minutes around the detected event) to the user's own **Google Drive**, authenticated via OAuth the user grants directly to their own Google account — NomWatch never sees or stores Drive credentials on any third-party server, because there isn't one. Additional storage backends (S3-compatible, NAS/local-only, etc.) are a planned pluggable extension.

## What NomWatch explicitly does NOT do

- No NomWatch-operated cloud, relay, or account system of any kind.
- No telemetry phoning home.
- No public exposure of any camera stream, ever, by default.
- No storage of credentials in the git repo, shipped package, or logs.

## Known trade-offs

- HLS over WebRTC costs ~1-3 seconds of latency in exchange for eliminating the ICE/ WebRTC interface-leak risk — acceptable for a monitoring/logging use case, not built for real-time interactive video.
- Tailscale is the default private-mesh backend because it's free for personal use, has automatic TLS, and needs no port-forwarding — but the bridge/network layer is designed to be swappable if a better privacy-preserving mesh option emerges.
