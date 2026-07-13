# NomWatch: Detection Reliability Overhaul

Paste this into a Claude Code session in this repo, running with the
highest available reasoning/thinking budget (Opus, max thinking). Give it
full autonomy to explore, test, and edit.

**Status check first**: the previous general web-app audit/overhaul prompt
already ran and shipped as commit `310d800` ("Web app audit + overhaul: fix
silent failures, add heartbeat/staleness/gallery, harden security"). Read
that commit and the current `docs/ROADMAP.md` before starting so you don't
duplicate work - the dashboard already has a heartbeat, staleness
detection, a clip gallery, config backup, camera troubleshooting, security
hardening, and mobile-responsive layout. This prompt is about a specific,
narrower, and now higher-priority problem that work did NOT touch:
**detection accuracy itself is not reliable.**

---

## The problem (confirmed live, 2026-07-12)

With the camera aimed at a genuinely empty room - nothing in frame, no pet,
no motion - the current pipeline (`nomwatch/detection.py`'s
`OllamaVisionDetector`, single still frame, `gemma3:4b`, one unconstrained
yes/no judgment per poll) produced confident FEEDING triggers with no
action on screen at all. The existing consecutive-poll debounce
(`consecutive_required`) only filters out *intermittent* noise - a single
bad frame among otherwise-correct ones. It does nothing when the model is
*consistently* wrong on a given camera's angle, lighting, or background,
which is what's happening here. This is now considered the most important
open problem in the project: a tool that cries wolf (or silently misses
real feeding events) defeats the entire point of building it.

Read `nomwatch/detection.py` in full before doing anything else. Note in
particular:
- `PROMPT` is a fixed constant with no context about the specific camera,
  the specific pet, or what "empty" looks like.
- `MotionOnlyDetector.check_frame` is currently `raise NotImplementedError`
  - a full stub, not a fallback.
- `DetectionConfig.pet_description` (in `config.py`) is saved by the web
  UI's screen 5 but never read anywhere in `detection.py` - it's dead data.
- There's no concept of a "zone" anywhere in the actual frame-capture/
  classify path, even though `webui.py`'s screen 2 has a
  zone-detection UI element - check whether the recent audit commit turned
  that into something real or left it a placeholder, and go from there.

---

## What to build

The product decision (already recorded in `docs/ROADMAP.md`'s new "v0.7 -
Detection reliability" section - read it) is to pursue all of these
together, not pick just one:

### 1. Real motion detection (`MotionOnlyDetector`)
Implement actual frame-diff motion detection - compare the current frame
against the previous one (grayscale, blur, absolute difference, threshold,
count of changed pixels/contour area over some percentage of frame), with
a sane default threshold and a config knob to tune it. This needs to work
standalone as a fully non-AI detection engine
(`detection.engine: "motion"`, no Ollama required at all) for anyone who
doesn't want to run a local model - make sure `cli.py`'s `run`/`detect-test`
commands and the web UI's detection-settings screen actually let someone
pick this path, not just the code existing unreferenced.

### 2. Motion-gating for the vision model
When using the `ollama` engine, don't invoke the vision model on every
poll regardless of scene content. Compare each captured frame to the
previous one first; only call the LLM when meaningful motion is detected.
An unchanging empty room should never reach the model at all - this is the
direct fix for the false positive that was observed. Think carefully about
the interaction with the existing consecutive-poll debounce and the
pre-roll clip timing (a "no motion, skip" cycle still needs to keep the
poll loop and heartbeat alive).

### 3. Hybrid corroboration mode
Add a mode (`detection.engine: "hybrid"`, or whatever naming fits the
existing config shape best) where a streak only advances when BOTH motion
is present AND the vision model says feeding - motion alone isn't
sufficient (a pet walking past without eating), vision alone is what
produced the empty-room false positive. Make this a real, selectable
option in both the CLI and the web UI, not just internal plumbing.

### 4. Zone cropping, wired for real
Turn the zone-detection placeholder into something real: a way to define a
bounding box (drawn over the live camera preview on screen 2, or the
dashboard - your call on the best UX) for where the feeder/bowl actually
is in frame, persisted to config, and applied as a crop to every frame
before BOTH the motion-diff comparison and the image sent to the vision
model. This cuts out background/lighting the model has no business
reasoning about and shrinks the motion detector's exposure to irrelevant
scene changes (shadows crossing a wall, a TV in the background, etc.).

### 5. Wire `pet_description` into the real prompt
`cfg.detection.pet_description` (e.g. "black cat", "golden retriever named
Max") is currently saved and unused. Fold it into the vision model's
prompt as a concrete anchor ("you are looking for a black cat specifically
- if what's in frame doesn't match that, treat it as not-feeding") to
reduce false positives from irrelevant motion and reduce the model's
tendency to guess when uncertain.

### 6. (If time allows) Setup-time calibration
Add an optional step - could live in the setup wizard or be a CLI command
(`nomwatch calibrate`?) - that captures N frames of the actual empty
feeder on the user's real camera, runs real classifications on them, and
reports the model's baseline false-positive rate, suggesting a
`min_confidence`/`consecutive_required` that would have suppressed what it
just saw. This is the single most direct way to catch "this specific
camera/lighting/model combo is unreliable" before the user ever gets a
false alert in production.

---

## Constraints (same as always - do not violate)

1. No NomWatch-operated cloud/backend/relay, ever. No paid tiers, ever.
   Everything here runs entirely on the user's own machine with their own
   local model.
2. Don't generalize past "feeding event at a pet feeder" into a
   configurable event-type system - zone/motion/pet-description are all in
   service of detecting feeding more reliably, not detecting other things.
3. Preserve the existing visual design system in `webui.py` (CSS variables,
   card layout) - extend it for any new UI (zone picker, engine-mode
   selector), don't redesign what already works.
4. Every detection engine choice (`ollama` / `motion` / `hybrid`) needs to
   keep working with everything downstream that already exists and was
   just hardened: the heartbeat file, the staleness/restart detection, the
   clip pre-roll/post-confirm pipeline, notifications, and all three
   storage backends. Don't let this become a parallel code path that only
   half-integrates with the rest of the app.

---

## How to verify (this is the part that matters most)

Static code review is not enough here - the entire premise of this prompt
is that the current code looked correct and still produced a false
positive on real hardware. For everything you build:

- Actually point the real camera at an empty room and confirm the new
  motion-gate/hybrid-mode genuinely suppresses the false positive that was
  observed - don't just reason about why it should.
- Actually trigger a real feeding-like scene (stage it, use a video, walk
  in frame, whatever's available) and confirm a real event still fires
  through to notification/clip/upload with the new engine active - a fix
  that kills false positives by also killing true positives is not a fix.
- If `pet_description` is set, verify the model's REASON text actually
  reflects using it (e.g. it should be able to say something like "this is
  a dog, not the described cat, so not counted").
- Report exact before/after: what the old pipeline did on the empty-room
  test, and what the new one does on the same test.
- If a live pass genuinely isn't possible for some part of this in this
  session, say exactly what wasn't verified rather than asserting it works.

---

## Git

Make as many intermediate commits as you want while working, but at the
end, squash everything from this session into exactly one commit on top of
the current history, with a clear, honest commit message summarizing what
changed and how it was verified. Push that single commit - no "wip" / "fix
again" trail in the pushed history.

## Final report

End with a plain-language summary for a non-engineer: what was actually
wrong with detection before, what changed, what the empty-room test showed
before vs. after, what's now selectable in the UI, and anything that still
needs more live testing than this session allowed for.
