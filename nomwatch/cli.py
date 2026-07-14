"""
NomWatch CLI - `nomwatch setup`, `nomwatch status`, `nomwatch doctor`.
"""
from __future__ import annotations

import datetime
import json
import os
import platform
import shutil
import time
from pathlib import Path

import click

from .bridge import binary_available, local_mediamtx_rtsp_url, rtsp_url, tailscale_status, write_mediamtx_config
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
    FrameDiffMotion,
    OllamaVisionDetector,
    Zone,
    capture_frame,
    crop_to_zone,
    list_local_models,
    model_installed,
    pick_vision_model,
    poll_stream,
    probe_local_model_server,
    pull_model,
)
from .notify import build_notifier
from . import monitorlock
from .service import (
    install_launchd_service,
    launchd_service_status,
    uninstall_launchd_service,
)
from .storage import build_storage_backend, find_google_drive_sync_folder


@click.group()
def main():
    """NomWatch: a free, open-source, privacy-first pet feeder camera bridge."""


@main.command("setup-legacy", hidden=True)
def setup_legacy():
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
    from .paths import NomWatchPaths
    from .state import LocalState
    state = LocalState(NomWatchPaths.from_environment())
    with state.connect() as conn:
        remote = conn.execute("SELECT desired_enabled,status FROM remote_access WHERE id=1").fetchone()
    if remote and (remote["desired_enabled"] or remote["status"] == "cleanup_required"):
        raise click.ClickException(
            "Remote Access still owns or may require cleanup of a Tailscale Serve mapping; "
            "disable/diagnose it before uninstalling the host service."
        )
    error = uninstall_launchd_service()
    click.echo(error or "Removed.")


@main.command()
def status():
    """Show current bridge/config health."""
    from .control import request as control_request
    from .paths import NomWatchPaths
    try:
        result = control_request(NomWatchPaths.from_environment().runtime / "control.sock", "status")
    except (OSError, ValueError, json.JSONDecodeError):
        result = None
    if result:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
        return
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
    """Grab live frame(s) from the configured camera and run a single detection pass."""
    cfg = load_config()
    if cfg is None:
        click.echo("No config found. Run `nomwatch setup` first.")
        return

    engine = cfg.detection.engine
    if engine not in ("ollama", "motion", "hybrid"):
        click.echo(
            f"Configured detection engine is '{engine}' (supported: ollama, motion, "
            "hybrid). Run `nomwatch setup`."
        )
        return
    if engine in ("ollama", "hybrid") and not cfg.detection.ollama_model:
        click.echo(
            f"Detection engine is '{engine}' but no vision model was picked. Install one "
            "(e.g. `ollama pull gemma3:4b`) and rerun `nomwatch setup`."
        )
        return

    stream_url = rtsp_url(cfg)
    zone = Zone.from_config(cfg.detection)
    zone_note = " (cropped to bowl zone)" if zone is not None else ""
    click.echo(
        f"[engine: {engine}] Capturing from "
        f"{cfg.camera.ip}:{cfg.camera.rtsp_port}/{cfg.camera.stream_path}{zone_note} ..."
    )
    frame = capture_frame(stream_url)
    if frame is None:
        click.echo(
            "Could not capture a frame. Check that ffmpeg is installed, the camera IP/creds "
            "are correct, and the bridge device can reach the camera on the LAN."
        )
        return
    analysed = crop_to_zone(frame, zone) if zone is not None else frame

    # Motion needs two frames; grab a second one a moment later.
    moved = motion_score = None
    if engine in ("motion", "hybrid"):
        motion = FrameDiffMotion(threshold=cfg.detection.motion_threshold)
        first = motion.thumbnail(analysed)
        time.sleep(1.0)
        frame2 = capture_frame(stream_url)
        analysed2 = crop_to_zone(frame2, zone) if (zone is not None and frame2) else frame2
        second = motion.thumbnail(analysed2) if analysed2 else None
        if first and second:
            mres = motion.compare(first, second)
            moved, motion_score = mres.moved, mres.score
            click.echo(f"Motion between two frames: score {motion_score:.2f} "
                       f"(threshold {cfg.detection.motion_threshold}) -> {'MOVED' if moved else 'still'}")
        else:
            click.echo("⚠️  Could not compute motion (frame capture/decode failed).")
        analysed = analysed2 or analysed  # classify the newer frame in hybrid

    if engine == "motion":
        if moved:
            click.echo(f"✅ MOTION detected near the bowl (score {motion_score:.2f}).")
        else:
            click.echo("❌ No motion detected.")
        return

    # ollama or hybrid: run the vision model.
    click.echo(f"Asking {cfg.detection.ollama_model} ...")
    detector = OllamaVisionDetector(
        model=cfg.detection.ollama_model,
        host=cfg.detection.ollama_host,
        min_confidence=cfg.detection.min_confidence,
        pet_description=cfg.detection.pet_description,
    )
    result = detector.classify(analysed)
    if result is None:
        click.echo("Could not reach the Ollama server to classify this frame.")
        return

    llm_positive = result.is_feeding and result.confidence >= cfg.detection.min_confidence
    if engine == "hybrid":
        if llm_positive and moved:
            click.echo(f"✅ FEEDING event (motion + vision agree, confidence {result.confidence:.2f}): {result.reason}")
        elif llm_positive and not moved:
            click.echo(f"❌ No feeding event: vision said feeding but nothing moved - suppressed (hybrid). {result.reason}")
        else:
            click.echo(f"❌ No feeding event (vision: not feeding, confidence {result.confidence:.2f}): {result.reason}")
    else:  # ollama
        if llm_positive:
            click.echo(f"✅ FEEDING event (confidence {result.confidence:.2f}): {result.reason}")
        else:
            verdict = "feeding, but below confidence threshold" if result.is_feeding else "not feeding"
            click.echo(f"❌ No feeding event ({verdict}, confidence {result.confidence:.2f}): {result.reason}")


@main.command("worker", hidden=True)
@click.option("--once", is_flag=True, help="Run a single debounced check cycle then exit (for testing) instead of looping forever.")
def worker(once: bool):
    """Host-owned detection worker. Not a public process-lifecycle command."""
    cfg = load_config()
    if cfg is None:
        click.echo("No config found. Run `nomwatch setup` first.")
        return

    engine = cfg.detection.engine
    if engine not in ("ollama", "motion", "hybrid"):
        click.echo(
            f"Configured detection engine is '{engine}', which `nomwatch run` "
            "doesn't drive (supported: ollama, motion, hybrid). Run `nomwatch setup`."
        )
        return
    if engine in ("ollama", "hybrid") and not cfg.detection.ollama_model:
        click.echo(
            f"Detection engine is '{engine}' but no vision model was picked during "
            "setup. Run `nomwatch setup` and install a vision model first."
        )
        return

    # This lock belongs to the monitoring process, not a particular UI. It
    # prevents a menu-bar app, web UI, terminal, or auto-start service from
    # running duplicate loops against the same camera.
    run_lock = monitorlock.run_loop_lock()
    if not run_lock.__enter__():
        run_lock.__exit__(None, None, None)
        click.echo("Monitoring is already running elsewhere; refusing to start a duplicate loop.")
        return

    supervised = os.environ.get("NOMWATCH_SUPERVISED") == "1"
    stream_url = local_mediamtx_rtsp_url(cfg) if supervised else rtsp_url(cfg)
    durable_state = None
    if supervised:
        from .paths import NomWatchPaths
        from .state import LocalState
        durable_state = LocalState(NomWatchPaths.from_environment())

    # The vision model is only needed for the ollama/hybrid engines.
    detector = None
    if engine in ("ollama", "hybrid"):
        detector = OllamaVisionDetector(
            model=cfg.detection.ollama_model,
            host=cfg.detection.ollama_host,
            min_confidence=cfg.detection.min_confidence,
            pet_description=cfg.detection.pet_description,
        )
    # Motion is needed for the motion engine, always for hybrid, and for
    # ollama only when motion-gating is on.
    motion = None
    if engine in ("motion", "hybrid") or (engine == "ollama" and cfg.detection.motion_gating):
        motion = FrameDiffMotion(threshold=cfg.detection.motion_threshold)
    zone = Zone.from_config(cfg.detection)

    notifier = build_notifier(cfg.notify)
    try:
        storage_backend = build_storage_backend(cfg.storage)
        storage_error = None
    except Exception as exc:  # noqa: BLE001 - a bad storage config must not kill monitoring
        # e.g. google_drive_sync chosen but the Drive for Desktop folder
        # doesn't exist (anymore). Previously this crashed the whole loop AT
        # STARTUP - the UI would say "started", the process died a second
        # later, and no feeding event was ever detected again. Fall back to
        # plain local storage so events/clips keep working, and surface the
        # problem in the heartbeat so the dashboard can show it.
        storage_backend = build_storage_backend(StorageConfig(provider="local"))
        storage_error = f"storage backend '{cfg.storage.provider}' failed ({exc}); falling back to local clips folder"
        click.echo(f"⚠️  {storage_error}")
    clips_dir = CONFIG_DIR / "clips"

    heartbeat_path = CONFIG_DIR / "heartbeat.json"
    classifications_path = CONFIG_DIR / "classifications.jsonl"
    flagged_frames_dir = CONFIG_DIR / "flagged_frames"

    def write_heartbeat(status: dict) -> None:
        """
        Atomic write so the dashboard never reads a half-written file, PLUS
        an append-only diagnostic log of every single classification the
        model makes (not just confirmed events) - this is what actually
        lets you figure out WHY a given camera/lighting setup is producing
        false positives, instead of just knowing that it did. When the
        model says "yes", the actual frame it was looking at is saved to
        disk too, so you can look at exactly what it thought was feeding.
        """
        frame_bytes = status.pop("frame_bytes", None)

        payload = {
            "ts": time.time(),
            "pid": os.getpid(),
            "poll_interval_seconds": cfg.detection.poll_interval_seconds,
            "storage_error": storage_error,
            **status,
        }
        tmp_path = heartbeat_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload))
        tmp_path.replace(heartbeat_path)

        # Diagnostic log: every poll's raw judgment, appended forever (the
        # user can delete/rotate this file any time - it's not required for
        # the app to function, purely for figuring out why the model is
        # wrong). Only meaningful polls get an entry (skip pure "ok: False"
        # transport errors already visible in heartbeat/logs).
        if status.get("ok"):
            frame_path = None
            if frame_bytes:
                flagged_frames_dir.mkdir(parents=True, exist_ok=True)
                frame_path = flagged_frames_dir / f"{int(payload['ts'])}.jpg"
                frame_path.write_bytes(frame_bytes)
                # Keep this directory from growing forever - false positives
                # during tuning can happen every few minutes for hours.
                existing = sorted(flagged_frames_dir.glob("*.jpg"))
                for stale in existing[:-100]:
                    stale.unlink(missing_ok=True)

            with open(classifications_path, "a") as f:
                f.write(json.dumps({
                    "ts": payload["ts"],
                    "engine": status.get("engine"),
                    "is_feeding": status.get("is_feeding"),
                    "confidence": status.get("confidence"),
                    "reason": status.get("reason"),
                    "raw_text": status.get("raw_text"),
                    "moved": status.get("moved"),
                    "motion_score": status.get("motion_score"),
                    "gated": status.get("gated"),
                    "streak": status.get("streak"),
                    "frame_path": str(frame_path) if frame_path else None,
                }) + "\n")

    log_path = CONFIG_DIR / "events.jsonl"
    engine_desc = {
        "ollama": f"vision model ({cfg.detection.ollama_model})"
                  + (", motion-gated" if motion is not None else ""),
        "motion": "motion only (no AI)",
        "hybrid": f"motion + vision model ({cfg.detection.ollama_model}) must agree",
    }[engine]
    zone_desc = " | zone-cropped to bowl area" if zone is not None else ""
    click.echo(
        f"Watching {cfg.camera.ip} every {cfg.detection.poll_interval_seconds}s "
        f"[engine: {engine_desc}{zone_desc}], "
        f"requiring {cfg.detection.consecutive_required} consecutive positive checks "
        f"(~{cfg.detection.consecutive_required * cfg.detection.poll_interval_seconds}s of eating) "
        f"before logging an event. Log: {log_path}\n"
        f"Notifications: {cfg.notify.provider} | Storage: {cfg.storage.provider}\n"
        "(Ctrl+C to stop)\n"
    )

    try:
        events = poll_stream(
            stream_url,
            engine=engine,
            detector=detector,
            motion=motion,
            motion_gating=cfg.detection.motion_gating,
            zone=zone,
            interval_seconds=cfg.detection.poll_interval_seconds,
            consecutive_required=cfg.detection.consecutive_required,
            rearm_after_negative_polls=cfg.detection.rearm_after_negative_polls,
            on_poll=write_heartbeat,
        )
        for event in events:
            ts = datetime.datetime.fromtimestamp(event.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            click.echo(f"🐾 [{ts}] Feeding event confirmed - confidence {event.confidence:.2f}: {event.reasoning}")

            if durable_state is not None:
                run_at = (
                    event.timestamp + cfg.detection.clip_post_confirm_seconds
                    + (cfg.bridge.record_segment_seconds + 1 if cfg.detection.pre_roll_seconds > 0 else 0)
                )
                event_id = durable_state.persist_event_with_job(
                    timestamp=event.timestamp, confidence=event.confidence, reason=event.reasoning,
                    payload={"timestamp": event.timestamp, "confidence": event.confidence,
                             "reason": event.reasoning}, run_at=run_at,
                )
                click.echo(f"   Persisted event {event_id}; finalization queued.")
                if once:
                    break
                continue

            record = {
                "timestamp": event.timestamp,
                "confidence": event.confidence,
                "reasoning": event.reasoning,
                "clip_path": None,
                "drive_link": None,
                "notified": None,
                "error": None,
            }

            # Notify immediately at confirmation - don't make the user wait
            # for clip recording/upload to hear about it. Record whether the
            # push actually went through - a failed send used to vanish
            # without a trace.
            if notifier:
                record["notified"] = notifier.send(
                    "NomWatch: feeding detected",
                    f"Confidence {event.confidence:.2f}. {event.reasoning}",
                )
                if not record["notified"]:
                    click.echo("   ⚠️  Push notification failed to send (network/provider problem).")
                    record["error"] = "push notification failed to send"

            # Build the clip. If pre-roll is enabled, MediaMTX has been
            # continuously recording segments in the background the whole
            # time - wait out the post-confirm window, then stitch together
            # whichever segments cover [confirm_time - pre_roll, confirm_time
            # + post_confirm]. Otherwise, fall back to a simple forward-only
            # recording starting right now.
            if cfg.detection.clip_post_confirm_seconds > 0 or cfg.detection.pre_roll_seconds > 0:
                write_heartbeat({"ok": True, "phase": "recording clip"})
                if cfg.detection.pre_roll_seconds > 0:
                    from .config import clean_user_path

                    recordings_dir = Path(
                        clean_user_path(cfg.bridge.recordings_dir) or (CONFIG_DIR / "recordings")
                    )
                    click.echo(
                        f"   Waiting {cfg.detection.clip_post_confirm_seconds}s, then building a clip "
                        f"({cfg.detection.pre_roll_seconds}s pre-roll + "
                        f"{cfg.detection.clip_post_confirm_seconds}s post-confirm)..."
                    )
                    # Wait out the post-confirm window PLUS one full segment
                    # length: MediaMTX only finalizes a segment file when it
                    # rolls over to the next one, so sleeping exactly
                    # post_confirm seconds means the tail of the clip window
                    # is often still inside an unflushed, unreadable segment.
                    time.sleep(
                        cfg.detection.clip_post_confirm_seconds
                        + cfg.bridge.record_segment_seconds + 1
                    )
                    clip_path = build_clip_with_preroll(
                        recordings_dir,
                        "cam",
                        event.timestamp,
                        cfg.detection.pre_roll_seconds,
                        cfg.detection.clip_post_confirm_seconds,
                        out_dir=clips_dir,
                    )
                    if clip_path is None:
                        # No MediaMTX segments were found covering the needed
                        # window - most commonly because MediaMTX isn't
                        # actually continuously recording yet (just started,
                        # was started before pre-roll was enabled in config,
                        # or recordings_dir doesn't match what MediaMTX is
                        # writing to). Rather than silently giving up and
                        # uploading nothing, fall back to a plain forward-only
                        # recording so the user still gets SOMETHING.
                        click.echo(
                            "   ⚠️  No pre-roll segments found (check that MediaMTX is actually running "
                            "with recording enabled - restart it after changing detection settings). "
                            "Falling back to a post-confirm-only clip..."
                        )
                        record["error"] = "pre-roll segments not found; used post-confirm-only fallback"
                        clip_path = record_clip(
                            stream_url, cfg.detection.clip_post_confirm_seconds, out_dir=clips_dir
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
                        # Retry with backoff instead of one shot - clip
                        # storage is the whole point of the event, and a
                        # momentary Drive-sync hiccup shouldn't lose it.
                        last_exc = None
                        for attempt in range(3):
                            try:
                                link = storage_backend.upload_clip(clip_path)
                                record["drive_link"] = link
                                click.echo(f"   Uploaded: {link}")
                                last_exc = None
                                break
                            except Exception as exc:  # noqa: BLE001 - report, don't crash the loop
                                last_exc = exc
                                if attempt < 2:
                                    wait = 5 * (2 ** attempt)
                                    click.echo(f"   ⚠️  Upload attempt {attempt + 1} failed ({exc}), retrying in {wait}s...")
                                    time.sleep(wait)
                        if last_exc is not None:
                            click.echo(f"   ⚠️  Upload failed after 3 attempts: {last_exc}")
                            record["error"] = f"upload failed after 3 attempts: {last_exc}"
                    else:
                        click.echo("   (No storage backend configured - clip saved locally only, not uploaded.)")
                else:
                    click.echo("   ⚠️  Clip recording failed entirely (check ffmpeg/camera reachability).")
                    if not record["error"]:
                        record["error"] = "clip recording failed (see run.out.log)"

            with open(log_path, "a") as f:
                f.write(json.dumps(record) + "\n")

            if once:
                break
    except KeyboardInterrupt:
        click.echo("\nStopped.")
    finally:
        run_lock.__exit__(None, None, None)


@main.command()
def run():
    """Compatibility shim: ask the supervised host to start monitoring."""
    from .control import request as control_request
    from .paths import NomWatchPaths
    try:
        result = control_request(NomWatchPaths.from_environment().runtime / "control.sock", "monitoring.start")
    except OSError as exc:
        raise click.ClickException("NomWatch host is not running; start/install `nomwatch host` first") from exc
    click.echo(json.dumps(result, sort_keys=True))


@main.command(hidden=True)
def jobs():
    """Run the host-owned durable outbox worker."""
    from .jobs import run_job_worker
    run_job_worker()


@main.command()
@click.option("--port", default=5151, type=int)
def host(port: int):
    """Run the complete foreground NomWatch service (for launchd/systemd)."""
    from .host import run_host
    run_host(port)


@main.command()
@click.option("--port", default=None, type=int, help="Explicit loopback development server port.")
def ui(port: int | None):
    """Report the supervised host URL, or run an explicit loopback dev UI."""
    if port is None:
        from .control import request as control_request
        from .paths import NomWatchPaths
        try:
            control_request(NomWatchPaths.from_environment().runtime / "control.sock", "status")
        except OSError as exc:
            raise click.ClickException("NomWatch host is not running; start/install `nomwatch host` first") from exc
        click.echo("NomWatch is available at http://127.0.0.1:5151/")
        return
    try:
        from .webui import run_ui
    except ImportError:
        click.echo("The web UI requires Flask. Install it with: pip install nomwatch[ui]")
        return

    click.echo(f"Starting NomWatch UI at http://127.0.0.1:{port} (Ctrl+C to stop)")
    run_ui(port=port)


@main.command()
def setup():
    """Open the authenticated setup flow served by the running host."""
    from .control import request as control_request
    from .paths import NomWatchPaths
    try:
        control_request(NomWatchPaths.from_environment().runtime / "control.sock", "status")
    except OSError as exc:
        raise click.ClickException("NomWatch host is not running; start/install `nomwatch host` first") from exc
    click.echo("Open http://127.0.0.1:5151/setup in a browser.")


def _path_stats(path: Path) -> tuple[int, int]:
    """Returns (file_count, total_bytes) for a file or directory tree."""
    if path.is_file():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 1, 0
    files = 0
    size = 0
    for p in path.rglob("*"):
        if p.is_file():
            files += 1
            try:
                size += p.stat().st_size
            except OSError:
                pass
    return files, size


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def prune_targets(include_saved: bool):
    """The set of prunable event-data paths that currently exist."""
    targets = []  # (label, Path)
    candidates = [
        ("event log (events.jsonl)", CONFIG_DIR / "events.jsonl"),
        ("clips", CONFIG_DIR / "clips"),
        ("thumbnails", CONFIG_DIR / "thumbnails"),
        ("diagnostic log (classifications.jsonl)", CONFIG_DIR / "classifications.jsonl"),
        ("flagged frames", CONFIG_DIR / "flagged_frames"),
    ]
    if include_saved:
        from .config import clean_user_path
        cfg = load_config()
        if cfg and cfg.storage.local_save_dir:
            sd = clean_user_path(cfg.storage.local_save_dir)
            if sd:
                candidates.append(("external saved clips", Path(sd)))
    for label, p in candidates:
        if p.exists():
            targets.append((label, p))
    return targets


@main.command()
@click.option("--delete", "hard_delete", is_flag=True,
              help="PERMANENTLY delete instead of archiving to a recoverable folder.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--include-saved", is_flag=True,
              help="Also clear the external clip folder (storage.local_save_dir), not just NomWatch's internal data.")
def prune(hard_delete: bool, yes: bool, include_saved: bool):
    """Clear event history, clips, thumbnails and diagnostic logs.

    Useful after a bad tuning run (e.g. a batch of false positives). By default
    everything is MOVED into a timestamped archive folder under the config dir
    so it's recoverable; pass --delete to permanently remove it instead.
    """
    targets = prune_targets(include_saved)
    if not targets:
        click.echo("Nothing to prune - already clean.")
        return

    total_files = total_bytes = 0
    click.echo("Will clear:")
    for label, p in targets:
        files, size = _path_stats(p)
        total_files += files
        total_bytes += size
        click.echo(f"  - {label}: {files} file(s), {_human_size(size)}")
    action = "permanently delete" if hard_delete else "archive"
    click.echo(f"Total: {total_files} file(s), {_human_size(total_bytes)} to {action}.")

    if not yes and not click.confirm(
        f"{'PERMANENTLY DELETE' if hard_delete else 'Archive'} these now?", default=False
    ):
        click.echo("Aborted.")
        return

    archive_dir = None
    if not hard_delete:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_dir = CONFIG_DIR / "archive" / f"prune-{stamp}"
        archive_dir.mkdir(parents=True, exist_ok=True)

    for label, p in targets:
        if hard_delete:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        else:
            shutil.move(str(p), str(archive_dir / p.name))

    if hard_delete:
        click.echo(f"✅ Permanently deleted {total_files} file(s), freed {_human_size(total_bytes)}.")
    else:
        click.echo(f"✅ Archived {total_files} file(s) ({_human_size(total_bytes)}) to {archive_dir}")
        click.echo(f"   Recover with: mv {archive_dir}/* {CONFIG_DIR}/")
        click.echo(f"   Permanently delete later with: rm -rf '{archive_dir}'")


@main.command()
@click.option("--frames", "-n", default=15, help="How many frames to sample.")
@click.option("--interval", default=2.0, help="Seconds between frames.")
def calibrate(frames: int, interval: float):
    """Measure this camera's baseline false-positive rate on an EMPTY scene.

    Point the camera at the feeder with NO pet present, then run this. It
    samples several frames, checks how often the vision model wrongly says
    "feeding" and how much the scene drifts frame-to-frame, and suggests
    threshold settings tuned to THIS camera/lighting.
    """
    cfg = load_config()
    if cfg is None:
        click.echo("No config found. Run `nomwatch setup` first.")
        return

    stream_url = rtsp_url(cfg)
    zone = Zone.from_config(cfg.detection)
    detector = None
    if cfg.detection.ollama_model:
        detector = OllamaVisionDetector(
            model=cfg.detection.ollama_model,
            host=cfg.detection.ollama_host,
            min_confidence=cfg.detection.min_confidence,
            pet_description=cfg.detection.pet_description,
        )
    motion = FrameDiffMotion(threshold=cfg.detection.motion_threshold)

    click.echo(
        f"Calibrating on the CURRENT scene (make sure NO pet is present). "
        f"Sampling {frames} frames, ~{interval}s apart{' (zone-cropped)' if zone else ''}...\n"
    )
    prev_thumb = None
    motion_scores = []
    vision_total = 0
    vision_yes = 0
    fp_confidences = []
    for i in range(frames):
        frame = capture_frame(stream_url)
        if frame is None:
            click.echo(f"  frame {i+1}/{frames}: capture failed")
            time.sleep(interval)
            continue
        analysed = crop_to_zone(frame, zone) if zone is not None else frame
        thumb = motion.thumbnail(analysed)
        if prev_thumb is not None and thumb is not None:
            motion_scores.append(motion.compare(prev_thumb, thumb).score)
        if thumb is not None:
            prev_thumb = thumb
        line = f"  frame {i+1}/{frames}:"
        if detector is not None:
            res = detector.classify(analysed)
            if res is not None:
                vision_total += 1
                if res.is_feeding:
                    vision_yes += 1
                    fp_confidences.append(res.confidence)
                line += f" model says {'FEEDING' if res.is_feeding else 'no'} ({res.confidence:.2f})"
        if motion_scores:
            line += f"  motion={motion_scores[-1]:.2f}"
        click.echo(line)
        time.sleep(interval)

    click.echo("\n--- Results (empty scene, so any 'FEEDING' is a false positive) ---")
    noise_max = max(motion_scores) if motion_scores else 0.0
    noise_avg = sum(motion_scores) / len(motion_scores) if motion_scores else 0.0
    click.echo(f"Motion on a still scene: avg {noise_avg:.2f}, max {noise_max:.2f} (this is your noise floor).")
    suggested_threshold = max(1.0, round(noise_max * 4, 1))
    click.echo(f"-> Suggested motion_threshold: {suggested_threshold} (≈4x the noise floor; currently {cfg.detection.motion_threshold}).")

    if detector is None:
        click.echo("No vision model configured, so only motion was measured.")
    elif vision_total == 0:
        click.echo("Could not reach the vision model to measure its false-positive rate.")
    elif vision_yes == 0:
        click.echo(f"Vision model: 0/{vision_total} frames wrongly called feeding - reliable on this scene. 👍")
    else:
        rate = 100 * vision_yes / vision_total
        worst = max(fp_confidences)
        click.echo(f"⚠️  Vision model wrongly called feeding on {vision_yes}/{vision_total} frames ({rate:.0f}%), "
                   f"confidence up to {worst:.2f}.")
        click.echo("-> Recommend engine 'hybrid' or 'ollama' with motion-gating on, so these static-scene "
                   "false positives never count.")
        if worst < 0.95:
            click.echo(f"-> Or raise min_confidence above {worst:.2f} (currently {cfg.detection.min_confidence}).")
    click.echo("\nThese are suggestions - set them on the dashboard's detection settings, then restart monitoring.")


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
