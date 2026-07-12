"""
NomWatch CLI - `nomwatch setup`, `nomwatch status`, `nomwatch doctor`.
"""
from __future__ import annotations

import click

from .bridge import binary_available, tailscale_status, write_mediamtx_config
from .config import (
    BridgeConfig,
    CameraConfig,
    CONFIG_DIR,
    DetectionConfig,
    NomWatchConfig,
    load_config,
    save_config,
)
from .detection import (
    OllamaVisionDetector,
    capture_frame,
    list_local_models,
    pick_vision_model,
    probe_local_model_server,
)


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
    if probe_local_model_server(detection_cfg.ollama_host):
        models = list_local_models(detection_cfg.ollama_host)
        vision_model = pick_vision_model(models)
        if vision_model:
            click.echo(f"\nFound local Ollama server with vision-capable model: {vision_model}")
            detection_cfg.engine = "ollama"
            detection_cfg.ollama_model = vision_model
        else:
            click.echo(
                f"\nFound local Ollama server, but none of its models ({', '.join(models) or 'none'}) "
                "look vision-capable. Install one (e.g. `ollama pull gemma3:4b`) to use it here, "
                "or NomWatch will fall back to a bundled detector once that lands."
            )
            detection_cfg.engine = "motion"
    else:
        click.echo(
            "\nNo local Ollama server detected on "
            f"{detection_cfg.ollama_host} - NomWatch will fall back to its "
            "bundled/motion detector once that integration lands."
        )
        detection_cfg.engine = "motion"

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
    event = detector.check_frame(frame)
    if event:
        click.echo(f"✅ Feeding event detected (confidence {event.confidence:.2f}): {event.reasoning}")
    else:
        click.echo("No feeding event detected in this frame (or confidence below threshold).")


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
