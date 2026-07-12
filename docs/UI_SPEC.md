# Setup UI Spec

NomWatch's first-run and reconfiguration experience is moving from CLI
prompts (`nomwatch setup`) to a local web app. The CLI wizard stays as-is
underneath for power users, automation, and headless bridge devices - the
UI is a friendlier front end over the same config/backend logic, not a
replacement of it.

## Why a local web app

- Works cross-platform (macOS/Linux/Windows) with one codebase.
- Can look genuinely good (forms, sliders, copy-to-clipboard, QR codes) -
  things that are painful in a terminal.
- Reachable at `http://localhost:<port>` by default; could optionally be
  exposed over the existing Tailscale tunnel later for remote reconfiguration,
  same privacy posture as the camera stream itself.
- Much lower build/packaging cost than a native app (Electron/Tauri/Qt) while
  still being a "real app" from the user's perspective.

## Screens / steps

### 1. Camera + local model detection
- Camera IP/port/credentials/stream path (same fields as CLI today).
- Live "checking for a local model server..." status, mirroring today's
  Ollama probe.
- If a vision model is found: show which one, confirm and move on.
- If Ollama is running but no vision model: an "Install gemma3:4b" button
  with a real progress indicator (the CLI currently streams raw `ollama
  pull` output line-by-line to the terminal - the UI should turn that into
  a progress bar or at least a live-updating log panel), then a clear
  verified/confirmed state once done.
- If no Ollama at all: a clear message + link to https://ollama.com/download,
  with a "recheck" button rather than requiring a full wizard restart.

### 2. Clip timing
- Two sliders/number inputs: seconds of pre-roll (video before the pet
  arrives) and seconds of post-confirmation recording. Live-updating text
  showing the derived debounce math (poll interval, consecutive checks
  required, total clip duration) so it's not just raw numbers with no
  context.

### 3. Storage
This needs to support **two independent destinations**, not one combined
choice, since someone might want local-only pre-roll buffering with
cloud-only final delivery (or vice versa):

- **Pre-roll cache location** (only shown if pre-roll seconds > 0): local
  folder only - this is always local, since it's a rolling buffer, never
  uploaded itself. Folder picker (native OS dialog if we're in a
  desktop-wrapped context, otherwise a text path field with a "browse"
  affordance where the browser/OS allows it).
- **Final event clip destination**, with independent choice of:
  - Local folder only (with its own folder picker, can be the same or a
    different folder than the pre-roll cache)
  - Google Drive via Drive for Desktop sync folder (auto-detected, as today)
  - Google Drive via direct OAuth API (advanced option, as today)
  - Both local AND Drive (save locally, then also upload)
  - None

### 4. Notifications
- Explain what ntfy is in plain language (not just "pick a topic name").
- Direct links/badges for: iOS App Store, Google Play, and the ntfy web app
  (https://ntfy.sh/app), so the user can tap straight through on whatever
  device they're setting this up from.
- A generated topic name by default (random, hard to guess) shown with a
  one-tap copy button, PLUS a QR code encoding the subscribe URL
  (`https://ntfy.sh/<topic>`) so someone can just scan it on their phone
  instead of typing anything.
- Option to type in a custom topic name instead of the generated one.
- Pushover as an alternate/secondary option, same as today.

### 5. (Optional, off by default) Appearance identification
Since a frame is already being sent to the local vision model to answer
"is this a feeding event," it's nearly free to also ask it to describe what
it sees - species, color, rough size, etc. - and store that alongside the
event. This is an *enrichment* of the existing feeding-event record, not a
new detection category (see the explicit non-goal below). UI: a toggle,
off by default, "Also describe what NomWatch sees" - if on, the event log
and any notification text include a short appearance description alongside
the feeding confirmation.

## Explicit non-goal (recorded, not built)

**Generalizing beyond "feeding event" to arbitrary configurable event
types** (e.g. presence detection, package delivery, intrusion alerts) is
NOT in scope for v1/v1.x and should not be implemented. The architecture
likely *could* generalize this way eventually - the detection engine
already just classifies a frame against a prompt - but NomWatch's product
identity for now is specifically a pet feeder monitor. This is recorded
here so the idea isn't lost, but no code should branch on "event type"
today. If this changes later, it deserves its own product-scope
conversation, not a quiet feature creep.

## Non-UI implication: config file needs two storage destinations

`StorageConfig` currently models one destination. Supporting the
independent pre-roll-cache-vs-final-clip-destination split above will need
a small config model change (e.g. splitting into
`pre_roll_cache_dir` under `BridgeConfig`, already effectively true today
via `recordings_dir`, plus allowing `StorageConfig` to represent "local AND
Drive" rather than a single enum choice) - tracked as an implementation
detail once the UI work actually starts building screen 3.
