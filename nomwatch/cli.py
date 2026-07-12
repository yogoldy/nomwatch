"""
NomWatch CLI - `nomwatch setup`, `nomwatch status`, `nomwatch doctor`.
"""
from __future__ import annotations

import datetime
import json

import click

from .bridge import binary_available, tailscale_status, write_mediamtx_config
from .clip import record_clip
from .config import (
    BridgeConfig,
    CameraConfig,
    CONFIG_DIR,
    DetectionConfig,
    NomWatchConfig,
    NotifyConfig,
    StorageConfig,
    load_config,
    save_config,
)
from .detection import (
    DEFAULT_VISION_MODEL,
    OllamaVisionDetector,
    capture_frame,
    list_local_models,
    model_installed,
    pick_vision_model,
    probe_local_model_server,
    pull_model,
)
from .notify import build_notifier
from .storage import build_storage_backend


@click.group()
def main():
    """NomWatch: a free, open-source, privacy-first pet feeder camera bridge."""


@main.command()
def setup():
    """Interactive setup wizard: camera details, Tailscale check, bridge config."""
    click.echo("NomWatch setup\n")

    ip = click.prompt("Camera LAN IP address (e.g. 192.168.1.250)")
    rtsp_port = click.prompt("Camera RTSP port", default=554, type=int)
    username = click.prompt("Camera-account username")
    password = click.prompt("Camera-account password", hide_input=True)
    stream_path = click.prompt("Stream path", default="stream1")

    detection_cfg = DetectionConfig()

    detection_cfg.poll_interval_seconds = click.prompt(
        "How often should the camera be checked, in seconds?",
        default=detection_cfg.poll_interval_seconds,
        type=int,
    )
    detection_cfg.required_eating_seconds = click.prompt(
        "How many seconds of continuous eating behavior before you want a notification? "
        "(higher = fewer false alerts, more delay)",
        default=detection_cfg.required_eating_seconds,
        type=int,
    )
    detection_cfg.consecutive_required = max(
        1, round(detection_cfg.required_eating_seconds / detection_cfg.poll_interval_seconds)
    )
    click.echo(
        f"-> Will require {detection_cfg.consecutive_required} consecutive positive checks "
        f"in a row (~{detection_cfg.consecutive_required * detection_cfg.poll_interval_seconds}s) "
        "before notifying."
    )

    click.echo("\nChecking for a local detection model (Ollama)...")
    if probe_local_model_server(detection_cfg.ollama_host):
        models = list_local_models(detection_cfg.ollama_host)
        vision_model = pick_vision_model(models)

        if vision_model:
            click.echo(f"Found local Ollama server with vision-capable model: {vision_model}")
            detection_cfg.engine = "ollama"
            detection_cfg.ollama_model = vision_model

        else:
            click.echo(
                f"Found local Ollama server, but none of its installed models "
                f"({', '.join(models) or 'none'}) look vision-capable."
            )
            if click.confirm(
                f"Install the recommended vision model ({DEFAULT_VISION_MODEL}) now via "
                "`ollama pull`?", default=True
            ):
                click.echo(f"Pulling {DEFAULT_VISION_MODEL} - this can take a few minutes...\n")
                pulled_ok = pull_model(DEFAULT_VISION_MODEL, on_output=click.echo)

                # Verify it's actually there, don't just trust the exit code.
                models_after = list_local_models(detection_cfg.ollama_host)
                if pulled_ok and model_installed(models_after, DEFAULT_VISION_MODEL):
                    click.echo(f"\n✅ Verified: {DEFAULT_VISION_MODEL} is installed and ready.")
                    detection_cfg.engine = "ollama"
                    detection_cfg.ollama_model = DEFAULT_VISION_MODEL
                else:
                    click.echo(
                        f"\n⚠️  Could not verify {DEFAULT_VISION_MODEL} is installed after the pull. "
                        f"Try `ollama pull {DEFAULT_VISION_MODEL}` manually, then rerun `nomwatch setup`."
                    )
                    detection_cfg.engine = "motion"
            else:
                click.echo(
                    f"Skipping install. Run `ollama pull {DEFAULT_VISION_MODEL}` manually "
                    "and rerun `nomwatch setup` whenever you're ready."
                )
                detection_cfg.engine = "motion"

    else:
        click.echo(
            f"No local Ollama server detected on {detection_cfg.ollama_host}. "
            "Install and start Ollama first (https://ollama.com/download), then rerun "
            "`nomwatch setup` - NomWatch will fall back to its bundled/motion detector "
            "until then."
        )
        detection_cfg.engine = "motion"

    detection_cfg.clip_post_confirm_seconds = click.prompt(
        "How many seconds of clip should be recorded once feeding is confirmed? "
        "(recording starts at confirmation, not before - no pre-roll buffer yet)",
        default=detection_cfg.clip_post_confirm_seconds,
        type=int,
    )

    notify_cfg = NotifyConfig()
    if click.confirm("\nSet up push notifications now (via ntfy.sh, free, no account needed)?", default=True):
        notify_cfg.provider = "ntfy"
        notify_cfg.ntfy_topic = click.prompt(
            "Pick an ntfy topic name (make it hard to guess - anyone who knows it can read your "
            "notifications, e.g. 'nomwatch-yourname-8f3k2')"
        )
        click.echo(
            f"-> Subscribe to this topic in the ntfy app (iOS/Android) or at "
            f"https://ntfy.sh/{notify_cfg.ntfy_topic} to receive alerts."
        )
    else:
        notify_cfg.provider = "none"

    storage_cfg = StorageConfig()
    if click.confirm("\nUpload event clips to Google Drive?", default=True):
        storage_cfg.provider = "google_drive"
        if click.confirm("Do you have a Drive folder ID to upload into (optional)?", default=False):
            storage_cfg.drive_folder_id = click.prompt("Drive folder ID")
        click.echo(
            "-> One-time setup required before this works: see docs/GOOGLE_DRIVE_SETUP.md "
            "to create a free OAuth client and save it to ~/.config/nomwatch/drive_credentials.json"
        )
    else:
        storage_cfg.provider = "none"

    cfg = NomWatchConfig(
        camera=CameraConfig(
            ip=ip,
            rtsp_port=rtsp_port,
            username=username,
            password=password,
            stream_path=stream_path,
        ),
        bridge=BridgeConfig(),
        detection=detection_cfg,
        notify=notify_cfg,
        storage=storage_cfg,
    )

    path = save_config(cfg)
    click.echo(f"\nSaved config to {path} (permissioned 600, owner-only)")

    if binary_available("tailscale"):
        click.echo("Tailscale detected on this device.")
        status = tailscale_status()
        if status:
            click.echo(status)
    else:
        click.echo(
            "Tailscale not found. Install it (https://tailscale.com/download) "
            "and log in before running `nomwatch bridge up`."
        )

    if binary_available("mediamtx"):
        click.echo("MediaMTX detected.")
    else:
        click.echo(
            "MediaMTX not found. Install via `brew install mediamtx` "
            "(or see https://github.com/bluenviron/mediamtx) before running `nomwatch bridge up`."
        )

    mediamtx_conf_path = CONFIG_DIR / "mediamtx.yml"
    write_mediamtx_config(cfg, mediamtx_conf_path)
    click.echo(f"Generated MediaMTX config at {mediamtx_conf_path}")

    click.echo("\nSetup complete. Run `nomwatch detect-test` to try a live detection pass. "
                "Notifications and storage wiring land in v0.4.")


@main.command()
def status():
    """Show current bridge/config health."""
    cfg = load_config()
    if cfg is None:
        click.echo("No config found. Run `nomwatch setup` first.")
        return

    click.echo(f"Camera: {cfg.camera.ip}:{cfg.camera.rtsp_port}/{cfg.camera.stream_path}")
    click.echo(f"MediaMTX binary present: {binary_available('mediamtx')}")
    click.echo(f"Tailscale binary present: {binary_available('tailscale')}")
    if binary_available("tailscale"):
        click.echo(tailscale_status() or "(no status output)")


@main.command()
def detect_test():
    """Grab one live frame from the configured camera and run a single detection pass."""
    cfg = load_config()
    if cfg is None:
        click.echo("No config found. Run `nomwatch setup` first.")
        return

    if cfg.detection.engine != "ollama" or not cfg.detection.ollama_model:
        click.echo(
            f"Configured detection engine is '{cfg.detection.engine}', not 'ollama', "
            "or no vision model was picked during setup. Nothing to test yet - "
            "install a vision model (e.g. `ollama pull gemma3:4b`) and rerun `nomwatch setup`."
        )
        return

    stream_url = (
        f"rtsp://{cfg.camera.username}:{cfg.camera.password}@"
        f"{cfg.camera.ip}:{cfg.camera.rtsp_port}/{cfg.camera.stream_path}"
    )
    click.echo(f"Capturing a frame from {cfg.camera.ip}:{cfg.camera.rtsp_port}/{cfg.camera.stream_path} ...")
    frame = capture_frame(stream_url)
    if frame is None:
        click.echo(
            "Could not capture a frame. Check that ffmpeg is installed, the camera IP/creds "
            "are correct, and the bridge device can reach the camera on the LAN."
        )
        return

    click.echo(f"Captured {len(frame)} bytes. Asking {cfg.detection.ollama_model} ...")
    detector = OllamaVisionDetector(
        model=cfg.detection.ollama_model,
        host=cfg.detection.ollama_host,
        min_confidence=cfg.detection.min_confidence,
    )
    result = detector.classify(frame)
    if result is None:
        click.echo("Could not reach the Ollama server to classify this frame.")
        return

    if result.is_feeding and result.confidence >= cfg.detection.min_confidence:
        click.echo(f"✅ FEEDING event (confidence {result.confidence:.2f}): {result.reason}")
    else:
        verdict = "feeding, but below confidence threshold" if result.is_feeding else "not feeding"
        click.echo(f"❌ No feeding event ({verdict}, confidence {result.confidence:.2f}): {result.reason}")


@main.command()
@click.option("--once", is_flag=True, help="Run a single debounced check cycle then exit (for testing) instead of looping forever.")
def run(once: bool):
    """Continuously watch the camera and log feeding events (Ctrl+C to stop)."""
    cfg = load_config()
    if cfg is None:
        click.echo("No config found. Run `nomwatch setup` first.")
        return

    if cfg.detection.engine != "ollama" or not cfg.detection.ollama_model:
        click.echo(
            f"Configured detection engine is '{cfg.detection.engine}', not 'ollama', "
            "or no vision model was picked during setup. Run `nomwatch setup` and install "
            "a vision model first."
        )
        return

    stream_url = (
        f"rtsp://{cfg.camera.username}:{cfg.camera.password}@"
        f"{cfg.camera.ip}:{cfg.camera.rtsp_port}/{cfg.camera.stream_path}"
    )
    detector = OllamaVisionDetector(
        model=cfg.detection.ollama_model,
        host=cfg.detection.ollama_host,
        min_confidence=cfg.detection.min_confidence,
    )

    notifier = build_notifier(cfg.notify)
    storage_backend = build_storage_backend(cfg.storage)
    clips_dir = CONFIG_DIR / "clips"

    log_path = CONFIG_DIR / "events.jsonl"
    click.echo(
        f"Watching {cfg.camera.ip} every {cfg.detection.poll_interval_seconds}s, "
        f"requiring {cfg.detection.consecutive_required} consecutive positive checks "
        f"(~{cfg.detection.consecutive_required * cfg.detection.poll_interval_seconds}s of eating) "
        f"before logging an event. Log: {log_path}\n"
        f"Notifications: {cfg.notify.provider} | Storage: {cfg.storage.provider}\n"
        "(Ctrl+C to stop)\n"
    )

    events = detector.poll_stream(
        stream_url,
        interval_seconds=cfg.detection.poll_interval_seconds,
        consecutive_required=cfg.detection.consecutive_required,
    )
    try:
        for event in events:
            ts = datetime.datetime.fromtimestamp(event.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            click.echo(f"🐾 [{ts}] Feeding event confirmed - confidence {event.confidence:.2f}: {event.reasoning}")

            record = {
                "timestamp": event.timestamp,
                "confidence": event.confidence,
                "reasoning": event.reasoning,
                "clip_path": None,
                "drive_link": None,
            }

            # Notify immediately at confirmation - don't make the user wait
            # for clip recording/upload to hear about it.
            if notifier:
                notifier.send(
                    "NomWatch: feeding detected",
                    f"Confidence {event.confidence:.2f}. {event.reasoning}",
                )

            # Record the post-confirmation clip (blocking - see clip.py for
            # why there's no pre-roll buffer yet).
            if cfg.detection.clip_post_confirm_seconds > 0:
                click.echo(f"   Recording {cfg.detection.clip_post_confirm_seconds}s clip...")
                clip_path = record_clip(
                    stream_url, cfg.detection.clip_post_confirm_seconds, out_dir=clips_dir
                )
                if clip_path:
                    record["clip_path"] = str(clip_path)
                    click.echo(f"   Clip saved: {clip_path}")

                    if storage_backend:
                        try:
                            link = storage_backend.upload_clip(clip_path)
                            record["drive_link"] = link
                            click.echo(f"   Uploaded: {link}")
                        except Exception as exc:  # noqa: BLE001 - report, don't crash the loop
                            click.echo(f"   ⚠️  Upload failed: {exc}")
                else:
                    click.echo("   ⚠️  Clip recording failed (check ffmpeg/camera reachability).")

            with open(log_path, "a") as f:
                f.write(json.dumps(record) + "\n")

            if once:
                break
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@main.command()
def doctor():
    """Check for required dependencies and report what's missing."""
    checks = {
        "tailscale": binary_available("tailscale"),
        "mediamtx": binary_available("mediamtx"),
        "ffmpeg": binary_available("ffmpeg"),
    }
    for name, ok in checks.items():
        click.echo(f"{'✅' if ok else '❌'} {name}")


if __name__ == "__main__":
    main()
