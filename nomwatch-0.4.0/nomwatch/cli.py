"""
NomWatch CLI - `nomwatch setup`, `nomwatch status`, `nomwatch doctor`.
"""
from __future__ import annotations

import datetime
import json
import platform
import time
from pathlib import Path

import click

from .bridge import binary_available, tailscale_status, write_mediamtx_config
from .clip import build_clip_with_preroll, record_clip
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
from .service import (
    install_launchd_service,
    launchd_service_status,
    uninstall_launchd_service,
)
from .storage import build_storage_backend, find_google_drive_sync_folder


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

    detection_cfg.pre_roll_seconds = click.prompt(
        "How many seconds of video BEFORE the pet arrives should clips include? "
        "(0 disables pre-roll and skips continuous local recording entirely)",
        default=detection_cfg.pre_roll_seconds,
        type=int,
    )
    detection_cfg.clip_post_confirm_seconds = click.prompt(
        "How many seconds of clip should be recorded AFTER feeding is confirmed?",
        default=detection_cfg.clip_post_confirm_seconds,
        type=int,
    )
    if detection_cfg.pre_roll_seconds > 0:
        click.echo(
            "-> MediaMTX will continuously record short local segments in the background so "
            "pre-roll is possible (auto-deleted after a couple minutes - this is not a full "
            "always-on recording, just a rolling buffer)."
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

    click.echo(
        "\nWhere should event clips go?\n"
        "  1) Local folder only - no cloud, no accounts, zero setup\n"
        "  2) Google Drive, via the Drive for Desktop app you already have installed/signed in "
        "(zero extra setup - just copies into its sync folder)\n"
        "  3) Google Drive, via direct API upload (advanced - requires creating your own free "
        "Google Cloud OAuth client, see docs/GOOGLE_DRIVE_SETUP.md)\n"
        "  4) None"
    )
    storage_choice = click.prompt("Choice", type=click.Choice(["1", "2", "3", "4"]), default="1")

    storage_cfg = StorageConfig()
    if storage_choice == "1":
        storage_cfg.provider = "local"
        click.echo(f"-> Clips will be saved to {CONFIG_DIR / 'clips'}")

    elif storage_choice == "2":
        storage_cfg.provider = "google_drive_sync"
        detected = find_google_drive_sync_folder()
        if detected:
            click.echo(f"-> Found Google Drive for Desktop sync folder: {detected}")
            storage_cfg.drive_sync_folder = str(detected)
        else:
            click.echo(
                "-> Couldn't auto-detect a Google Drive for Desktop sync folder. Make sure "
                "it's installed and signed in (https://www.google.com/drive/download/), or "
                "enter its 'My Drive' path manually now."
            )
            manual_path = click.prompt("Path (leave blank to configure later)", default="", show_default=False)
            if manual_path:
                storage_cfg.drive_sync_folder = manual_path

    elif storage_choice == "3":
        storage_cfg.provider = "google_drive_api"
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

    if platform.system() == "Darwin":
        if click.confirm(
            "\nAuto-start `nomwatch run` at login and keep it running in the background "
            "(via launchd, restarts automatically if it crashes)?", default=True
        ):
            error = install_launchd_service(CONFIG_DIR / "logs")
            if error:
                click.echo(f"⚠️  {error}")
            else:
                click.echo(
                    "✅ Installed and started. It'll now run automatically at login. "
                    "Check `nomwatch service-status` any time, or `nomwatch service-uninstall` "
                    "to remove it."
                )
        else:
            click.echo("Skipped. Run `nomwatch run` manually whenever you want it watching.")
    else:
        click.echo(
            "\nAuto-start service wiring is currently macOS-only (launchd). "
            "Run `nomwatch run` manually, or set up your own systemd unit for now."
        )

    click.echo("\nSetup complete. Run `nomwatch detect-test` to try a live detection pass.")


@main.command()
def service_status():
    """Show whether the launchd auto-start service is installed/running."""
    click.echo(launchd_service_status())


@main.command()
def service_uninstall():
    """Remove the launchd auto-start service (does not affect any config/data)."""
    error = uninstall_launchd_service()
    click.echo(error or "Removed.")


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

            # Build the clip. If pre-roll is enabled, MediaMTX has been
            # continuously recording segments in the background the whole
            # time - wait out the post-confirm window, then stitch together
            # whichever segments cover [confirm_time - pre_roll, confirm_time
            # + post_confirm]. Otherwise, fall back to a simple forward-only
            # recording starting right now.
            if cfg.detection.clip_post_confirm_seconds > 0 or cfg.detection.pre_roll_seconds > 0:
                if cfg.detection.pre_roll_seconds > 0:
                    recordings_dir = Path(cfg.bridge.recordings_dir or (CONFIG_DIR / "recordings"))
                    click.echo(
                        f"   Waiting {cfg.detection.clip_post_confirm_seconds}s, then building a clip "
                        f"({cfg.detection.pre_roll_seconds}s pre-roll + "
                        f"{cfg.detection.clip_post_confirm_seconds}s post-confirm)..."
                    )
                    time.sleep(cfg.detection.clip_post_confirm_seconds)
                    clip_path = build_clip_with_preroll(
                        recordings_dir,
                        "cam",
                        event.timestamp,
                        cfg.detection.pre_roll_seconds,
                        cfg.detection.clip_post_confirm_seconds,
                        out_dir=clips_dir,
                    )
                else:
                    click.echo(f"   Recording {cfg.detection.clip_post_confirm_seconds}s clip (no pre-roll)...")
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
