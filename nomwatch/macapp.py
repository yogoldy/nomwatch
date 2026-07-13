"""
Native macOS menu-bar app for NomWatch (v1.0).

A real AppKit app (pyobjc, no Electron/web-wrapper) that lives in the menu bar,
shows whether monitoring is active, can start/stop/restart it, keeps it alive
while running, and opens a native window ("the real app"). Startup-at-login is
a proper macOS Login Item (SMAppService) - NOT a launchd agent running a shell
script, which is the thing we're deliberately moving away from.

Runs as an "accessory" app (LSUIElement): menu-bar presence, no Dock icon.
Launch it with `nomwatch app`, or from the bundled NomWatch.app.

The detection loop itself is the same `nomwatch run` process the CLI/web UI
use (see runctl.py) - the app supervises it as a detached child, so quitting
the app does not kill an in-progress recording; the Login Item brings the app
(and monitoring) back after a reboot.
"""
from __future__ import annotations

import datetime
import json
import os
import stat
import sys
from pathlib import Path
from typing import List, Optional

from . import runctl
from .config import CONFIG_DIR, load_config

try:
    import objc
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSButton,
        NSColor,
        NSFont,
        NSMenu,
        NSMenuItem,
        NSObject,
        NSScrollView,
        NSStatusBar,
        NSTextField,
        NSTextView,
        NSVariableStatusItemLength,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskTitled,
    )
    from Foundation import NSMakeRect, NSTimer
    _PYOBJC_OK = True
    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001 - report a friendly message from the CLI
    _PYOBJC_OK = False
    _IMPORT_ERROR = exc


EVENTS_LOG_PATH = CONFIG_DIR / "events.jsonl"


def read_recent_events(limit: int = 15) -> List[dict]:
    """Most recent feeding events, newest first (from events.jsonl)."""
    if not EVENTS_LOG_PATH.exists():
        return []
    rows = []
    try:
        with open(EVENTS_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        return []
    return list(reversed(rows[-limit:]))


def _status_summary() -> dict:
    """Everything the UI needs about the current monitoring state, in one read."""
    hb = runctl.read_heartbeat()
    alive = runctl.monitoring_alive()
    external = runctl.external_run_pids()
    cfg = load_config()
    return {
        "alive": alive,
        "external": external,
        "heartbeat": hb,
        "engine": (cfg.detection.engine if cfg else None),
        "model": (cfg.detection.ollama_model if cfg else None),
        "config": cfg,
    }


def _icon_for(alive: bool, hb: Optional[dict]) -> str:
    """Menu-bar glyph: healthy green, stale/error red, stopped hollow."""
    if not alive:
        return "⚪"  # ⚪️ stopped
    if hb is None:
        return "\U0001f7e2"  # 🟢 just started, no heartbeat yet
    age = hb.get("age_seconds")
    poll = hb.get("poll_interval_seconds") or 10
    if hb.get("error") or (age is not None and age > max(30, poll * 4)):
        return "\U0001f534"  # 🔴 stale or erroring
    return "\U0001f7e2"  # 🟢 healthy


def _heartbeat_line(hb: Optional[dict]) -> str:
    if hb is None:
        return "No checks logged yet."
    age = hb.get("age_seconds")
    if hb.get("error"):
        return f"Problem: {hb['error']} ({age}s ago)"
    reason = (hb.get("reason") or "").strip()
    seen = f' - "{reason}"' if reason else ""
    verdict = "feeding!" if hb.get("is_feeding") else "no feeding"
    return f"Last check {age}s ago: {verdict}{seen}"


if _PYOBJC_OK:

    class NomWatchApp(NSObject):
        """NSApplicationDelegate: menu-bar item, supervisor timer, main window."""

        def init(self):
            self = objc.super(NomWatchApp, self).init()
            if self is None:
                return None
            self.should_monitor = False  # user intent, drives keep-alive
            self.status_item = None
            self.menu = None
            self.window = None
            self.win_status = None
            self.win_events = None
            self.win_login = None
            return self

        # --- lifecycle ---
        def applicationDidFinishLaunching_(self, notification):
            bar = NSStatusBar.systemStatusBar()
            self.status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
            self.status_item.button().setTitle_("⚪ NomWatch")
            self._build_menu()
            # Auto-start monitoring on launch (the "on startup" behavior) unless
            # something is already running.
            if not runctl.monitoring_alive():
                runctl.start_run_loop()
            self.should_monitor = True
            # Supervisor + UI refresh tick.
            self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                3.0, self, "tick:", None, True
            )
            self._refresh()
            if _open_on_launch:
                self.openWindow_(None)

        # --- menu ---
        @objc.python_method
        def _build_menu(self):
            menu = NSMenu.alloc().init()

            self.status_line = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Monitoring: ...", None, "")
            self.status_line.setEnabled_(False)
            menu.addItem_(self.status_line)

            self.heartbeat_line = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
            self.heartbeat_line.setEnabled_(False)
            menu.addItem_(self.heartbeat_line)

            menu.addItem_(NSMenuItem.separatorItem())

            self.mi_start = self._item(menu, "Start monitoring", "startClicked:")
            self.mi_restart = self._item(menu, "Restart monitoring", "restartClicked:")
            self.mi_stop = self._item(menu, "Stop monitoring", "stopClicked:")

            menu.addItem_(NSMenuItem.separatorItem())

            self._item(menu, "Open NomWatch", "openWindow:")
            self._item(menu, "Advanced setup (web)…", "openWebSetup:")

            self.mi_login = self._item(menu, "Start at login", "toggleLogin:")

            menu.addItem_(NSMenuItem.separatorItem())
            self._item(menu, "Quit NomWatch", "quitClicked:")

            self.status_item.setMenu_(menu)
            self.menu = menu

        @objc.python_method
        def _item(self, menu, title, selector):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, "")
            item.setTarget_(self)
            menu.addItem_(item)
            return item

        # --- timer / refresh ---
        def tick_(self, timer):
            # Keep-alive: if the user wants monitoring on but the loop died
            # (crash, reboot leftover), bring it back - the app IS the KeepAlive.
            if self.should_monitor and not runctl.monitoring_alive():
                runctl.start_run_loop()
            self._refresh()

        @objc.python_method
        def _refresh(self):
            s = _status_summary()
            self.status_item.button().setTitle_(f"{_icon_for(s['alive'], s['heartbeat'])} NomWatch")
            running = s["alive"]
            self.status_line.setTitle_("Monitoring: ACTIVE" if running else "Monitoring: stopped")
            self.heartbeat_line.setTitle_(_heartbeat_line(s["heartbeat"]))
            self.mi_start.setHidden_(running)
            self.mi_restart.setHidden_(not running)
            self.mi_stop.setHidden_(not running)
            if getattr(self, "mi_login", None) is not None:
                self.mi_login.setState_(1 if login_item_enabled() else 0)
            self._refresh_window(s)

        # --- actions ---
        def startClicked_(self, sender):
            self.should_monitor = True
            runctl.start_run_loop()
            self._refresh()

        def restartClicked_(self, sender):
            self.should_monitor = True
            runctl.start_run_loop()  # start_run_loop restarts if already running
            self._refresh()

        def stopClicked_(self, sender):
            self.should_monitor = False
            runctl.stop_run_loop()
            self._refresh()

        def quitClicked_(self, sender):
            # Leave the detached monitoring loop running; just quit the UI.
            NSApplication.sharedApplication().terminate_(self)

        def toggleLogin_(self, sender):
            set_login_item(not login_item_enabled())
            self._refresh()

        def openWebSetup_(self, sender):
            import subprocess
            import shutil
            # Make sure the web UI is up, then open it in the browser.
            if not _web_ui_reachable():
                exe = shutil.which("nomwatch")
                cmd = [exe, "ui"] if exe else ["python3", "-m", "nomwatch.cli", "ui"]
                try:
                    subprocess.Popen(cmd, start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass
            subprocess.Popen(["open", "http://127.0.0.1:5151/setup"])

        # --- native main window ---
        def openWindow_(self, sender):
            if self.window is None:
                self._build_window()
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self.window.makeKeyAndOrderFront_(None)
            self._refresh()

        @objc.python_method
        def _build_window(self):
            w, h = 460, 480
            style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                     | NSWindowStyleMaskMiniaturizable)
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False
            )
            win.setTitle_("NomWatch")
            win.center()
            content = win.contentView()

            def label(text, x, y, width, size=13, bold=False):
                f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, width, 20))
                f.setStringValue_(text)
                f.setBezeled_(False)
                f.setDrawsBackground_(False)
                f.setEditable_(False)
                f.setSelectable_(False)
                f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
                content.addSubview_(f)
                return f

            label("NomWatch", 20, h - 40, 300, size=20, bold=True)
            self.win_status = label("Monitoring: ...", 20, h - 72, w - 40, size=14, bold=True)
            self.win_hb = label("", 20, h - 94, w - 40, size=11)

            def button(title, x, y, width, selector):
                b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, width, 30))
                b.setTitle_(title)
                b.setBezelStyle_(1)  # rounded
                b.setTarget_(self)
                b.setAction_(selector)
                content.addSubview_(b)
                return b

            button("Start", 20, h - 140, 100, "startClicked:")
            button("Restart", 130, h - 140, 100, "restartClicked:")
            button("Stop", 240, h - 140, 100, "stopClicked:")

            self.win_login = NSButton.alloc().initWithFrame_(NSMakeRect(20, h - 178, w - 40, 24))
            self.win_login.setButtonType_(3)  # switch/checkbox
            self.win_login.setTitle_("Start NomWatch automatically at login")
            self.win_login.setTarget_(self)
            self.win_login.setAction_("toggleLogin:")
            content.addSubview_(self.win_login)

            self.win_detection = label("", 20, h - 206, w - 40, size=11)

            label("Recent feeding events", 20, h - 240, 300, size=13, bold=True)
            scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 60, w - 40, h - 310))
            scroll.setHasVerticalScroller_(True)
            scroll.setBorderType_(2)  # bezel
            tv = NSTextView.alloc().initWithFrame_(scroll.contentView().bounds())
            tv.setEditable_(False)
            tv.setFont_(NSFont.userFixedPitchFontOfSize_(11))
            scroll.setDocumentView_(tv)
            content.addSubview_(scroll)
            self.win_events = tv

            button("Advanced setup (web)…", 20, 18, 200, "openWebSetup:")
            self.window = win

        @objc.python_method
        def _refresh_window(self, s):
            if self.window is None:
                return
            running = s["alive"]
            self.win_status.setStringValue_("Monitoring: ACTIVE" if running else "Monitoring: stopped")
            self.win_hb.setStringValue_(_heartbeat_line(s["heartbeat"]))
            eng = s.get("engine") or "?"
            model = s.get("model") or "-"
            cfg = s.get("config")
            extra = ""
            if cfg:
                extra = (f"  |  poll {cfg.detection.poll_interval_seconds}s"
                         f"  |  min-conf {cfg.detection.min_confidence}"
                         f"  |  motion {'on' if cfg.detection.motion_gating else 'off'}")
            self.win_detection.setStringValue_(f"Engine: {eng} ({model}){extra}")
            if self.win_login is not None:
                self.win_login.setState_(1 if login_item_enabled() else 0)
            if self.win_events is not None:
                events = read_recent_events(20)
                if not events:
                    text = "No feeding events yet."
                else:
                    lines = []
                    for e in events:
                        ts = e.get("timestamp")
                        when = (datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                                if ts else "?")
                        conf = e.get("confidence") or 0
                        reason = (e.get("reasoning") or "").strip()
                        lines.append(f"{when}  ({conf:.2f})  {reason}")
                    text = "\n".join(lines)
                self.win_events.setString_(text)


# --- Login Item (SMAppService), the non-launchd startup mechanism ------------

def _app_service():
    try:
        from ServiceManagement import SMAppService
        return SMAppService.mainAppService()
    except Exception:  # noqa: BLE001
        return None


def login_item_enabled() -> bool:
    svc = _app_service()
    if svc is None:
        return False
    try:
        return int(svc.status()) == 1  # SMAppServiceStatusEnabled
    except Exception:  # noqa: BLE001
        return False


def set_login_item(enabled: bool) -> Optional[str]:
    """Register/unregister this app as a macOS Login Item. Only meaningful when
    running from the bundled NomWatch.app; returns a message otherwise."""
    svc = _app_service()
    if svc is None:
        return "ServiceManagement unavailable."
    try:
        if enabled:
            ok, err = svc.registerAndReturnError_(None)
        else:
            ok, err = svc.unregisterAndReturnError_(None)
        if not ok:
            return f"Login Item change failed: {err}"
    except Exception as exc:  # noqa: BLE001 - bare-python (unbundled) can't register
        return f"Login Item requires the bundled NomWatch.app ({exc})."
    return None


def _web_ui_reachable() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 5151), timeout=0.5):
            return True
    except OSError:
        return False


_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>NomWatch</string>
    <key>CFBundleDisplayName</key>
    <string>NomWatch</string>
    <key>CFBundleIdentifier</key>
    <string>com.nomwatch.app</string>
    <key>CFBundleExecutable</key>
    <string>NomWatch</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <!-- Menu-bar-only: no Dock icon, no default main-menu window. -->
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
"""


def build_app_bundle(dest_dir: Path, python_exe: Optional[str] = None) -> Path:
    """
    Build a NomWatch.app bundle that launches the menu-bar app. The launcher
    execs the given Python interpreter (default: the current one) with the
    interpreter path QUOTED, so a venv under a directory with a space (like
    'Documents (local)/') works - the exact bug that broke the old launchd
    plist. This is an unsigned, machine-local bundle; a fully native launcher
    (py2app) for distribution is a follow-up.
    """
    python_exe = python_exe or sys.executable
    app = Path(dest_dir) / "NomWatch.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)

    (contents / "Info.plist").write_text(_INFO_PLIST)

    launcher = macos / "NomWatch"
    launcher.write_text(
        "#!/bin/sh\n"
        '# Quotes matter: the interpreter path may contain spaces.\n'
        f'exec "{python_exe}" -m nomwatch.cli app "$@"\n'
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return app


_open_on_launch = False


def run_app(open_window: bool = False) -> Optional[str]:
    """Entry point for `nomwatch app`. Returns an error string if it can't run."""
    if not _PYOBJC_OK:
        return (
            "The native macOS app needs pyobjc. Install it with:  "
            "pip install 'nomwatch[app]'\n"
            f"(import error: {_IMPORT_ERROR})"
        )
    global _open_on_launch
    _open_on_launch = open_window
    app = NSApplication.sharedApplication()
    # Accessory = menu-bar app, no Dock icon.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = NomWatchApp.alloc().init()
    app.setDelegate_(delegate)
    app.run()
    return None
