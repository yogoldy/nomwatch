# NomWatch 🐾

**A free, open-source, privacy-first camera bridge that turns any cheap LAN camera into a smart pet feeder monitor — without a subscription, without the cloud vendor, and without $60/month for a "camera feeder."**

NomWatch watches your existing pet feeder (or any LAN camera pointed at one), detects feeding events, logs them, pings your phone, and archives a short clip to your own cloud storage — all running locally on hardware you already own, reachable only by you.

## Why this exists

Camera-enabled pet feeders lock your video behind a vendor cloud, a subscription, and closed firmware. NomWatch flips that: any RTSP/ONVIF camera (Tapo, Wyze in RTSP mode, generic NVR cameras, etc.) becomes the "eyes," and an old laptop, Mac mini, or Raspberry Pi you already have becomes the smart layer — detecting feeding events and handling the rest.

## Core principles

1. **Security first.** No open ports, no port-forwarding, no public cloud relay for video. Access is scoped to a private network you control (Tailscale by default; pluggable for other private-mesh/VPN backends later).
2. **Free and open.** MIT-licensed. No paid tier, no "pro" gate.
3. **Bring your own hardware.** No proprietary camera required — any LAN camera with RTSP/ONVIF works.
4. **Bring your own cloud.** Google Drive today, pluggable storage backends later.
5. **Detect, don't just record.** Feeding events are identified (motion/object/pet detection), not just 24/7 raw footage.

## How it works (v1 architecture)

```
[LAN Camera] --RTSP--> [Bridge device: MediaMTX] --HLS (loopback only)--> [Tailscale serve, tailnet-only HTTPS]
                              |
                              v
                    [Detection engine] --event--> [Notification (ntfy/Pushover)] + [Clip upload (Google Drive)]
```

- **Bridge device**: any always-on machine on the same LAN as the camera (old MacBook, Raspberry Pi, Mac mini, NAS).
- **MediaMTX**: pulls RTSP from the camera, re-serves as HLS bound to loopback only.
- **Tailscale**: exposes the loopback stream at a private `*.ts.net` HTTPS URL, reachable only by devices on your tailnet. No public exposure, no funnel, no port-forwarding.
- **Detection engine**: pluggable. Prefers an existing video-analysis tool that can consume the same stream we've already built (e.g. Frigate, DeepStack, an existing NVR-style detector) over reinventing computer vision. On startup, NomWatch checks whether you have a local model server available (e.g. queries `ollama list`) and uses it if present; otherwise falls back to a lightweight local model (YOLOv8n-class) or basic motion detection as the minimum viable v1.
- **Notifications**: a free third-party push service (ntfy.sh or Pushover) — no custom iOS app or Apple Developer account required for v1.
- **Storage**: uploads short event clips to Google Drive (v1); other storage backends are a stretch goal.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full security model and [`docs/ROADMAP.md`](docs/ROADMAP.md) for the phased plan.

## Status

🚧 **Early development.** The Tailscale + MediaMTX bridge (the security-critical core) has been built and manually verified. The installable CLI, detection engine, notifications, and Drive upload are being scaffolded now — see the roadmap for what's done vs. planned.

## Quick start (target UX, not fully built yet)

```bash
pip install nomwatch
nomwatch host
```

The setup wizard will ask for:
- Your camera's LAN IP, RTSP port, and camera-account credentials
- Whether Tailscale is already installed/logged in on this device
- Your Google Drive folder (via OAuth) for event clips
- Your ntfy/Pushover details for notifications

Open the printed loopback claim URL, create the first owner, then finish camera
setup in the authenticated UI. LAN access remains off until the owner stages
and confirms a selected private interface. Optional Tailscale access is
configured from the Access page and always targets the authenticated gateway;
NomWatch never enables Funnel or public exposure.

For an installed service, use the exact macOS LaunchAgent or Raspberry Pi OS
systemd artifacts described in [the operator guide](docs/OPERATOR_GUIDE.md).

## Contributing

Issues and PRs welcome. This is meant to be a genuinely community-owned alternative to paid pet-cam hardware — if you've got a camera or detection model this doesn't support yet, open an issue.

## License

MIT — see [`LICENSE`](LICENSE).
