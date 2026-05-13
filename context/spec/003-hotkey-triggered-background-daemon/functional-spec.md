# Functional Specification: Hotkey-Triggered Background Daemon

- **Roadmap Item:** Hotkey-triggered background daemon (Phase 1 — Capture Foundation)
- **Status:** Draft
- **Author:** e

---

## 1. Overview and Rationale (The "Why")

The first two Phase 1 specs (`001-mixed-mic-system-audio-capture` and `002-primary-display-video-capture`) defined what a capture session captures: a `.wav` audio file and a `.mp4` video file produced by a single `record start` / `record stop` pair in the terminal. That is enough to prove the capture works, but it is not how the consultant will actually live with the product. Reaching for a terminal in the middle of joining a client call breaks flow, and a recording missed in the first thirty seconds of a call is a recording that doesn't get made.

This specification removes the terminal from the everyday path. From this spec forward, a background process — the **daemon** — is always running while the user is logged in, listening for a single global keyboard shortcut. Pressing the shortcut starts a capture; pressing it again stops it. The user records a meeting without switching apps, without typing a command, and without any visible window or menu bar item belonging to this product.

The product's "zero meeting-side footprint" rule is unchanged: the shortcut is intercepted by macOS before any meeting client sees it, so other participants in Zoom, Google Meet, or Microsoft Teams are never notified.

**Success measure:** Across a normal working day, the user starts and stops every meeting recording entirely from the keyboard shortcut, never opens a terminal, and at the end of the day finds all recordings in the configured folder, each one paired (audio + video) and time-stamped — with no awareness, during the meeting, of anything happening on the screen.

---

## 2. Functional Requirements (The "What")

### 2.1 What "the daemon" is, from the user's point of view

- **As the user, I want a background helper that is always quietly running and ready, so that starting a recording is instantaneous and never requires me to launch anything.**
  - **Acceptance Criteria:**
    - [ ] While the daemon is running, it has **no menu bar icon**, **no Dock icon**, and **no application window** — there is nothing on screen indicating it exists.
    - [ ] The daemon does not steal focus, does not bring any window forward, and does not interact with the meeting client in any way.
    - [ ] At any given moment, the daemon is either idle (no capture in progress) or recording (exactly one capture session in progress). It never holds two captures at once.

### 2.2 One-time installation

- **As the user, I want a single setup step that registers the daemon to start automatically when I log in, so that I never have to remember to launch it.**
  - **Acceptance Criteria:**
    - [ ] Running `record install` in the terminal registers the daemon to start automatically every time the user logs into macOS, and starts the daemon immediately (so the user does not have to log out and back in).
    - [ ] Running `record install` a second time is safe (no errors, no duplicates) — it leaves the daemon registered and running exactly once.
    - [ ] Running `record uninstall` removes the auto-start registration and stops any currently running daemon. After uninstall, the user can run `record install` again to re-register.
    - [ ] `record install` and `record uninstall` complete in the foreground and print a clear confirmation message naming what was done.

### 2.3 Manual control of the daemon

- **As the user, I want to be able to start, stop, and restart the daemon by hand, so that I can recover from a wedged daemon or pick up a config change without rebooting.**
  - **Acceptance Criteria:**
    - [ ] Running `record daemon start` launches the daemon for the current login session. If the daemon is already running, the command prints a message saying so and exits successfully.
    - [ ] Running `record daemon stop` stops the running daemon. If a capture is currently in progress, the capture is ended cleanly first (both files finalized and saved, exactly as if the user had pressed the hotkey to stop), and only then is the daemon shut down.
    - [ ] Running `record daemon restart` is equivalent to `record daemon stop` followed by `record daemon start`. If a capture was in progress, it is finalized before the daemon restarts; the new daemon starts in the idle state.
    - [ ] None of these commands change the auto-start-on-login registration set by `record install`.

### 2.4 Inspecting the daemon

- **As the user, I want to ask the daemon what it's doing right now, so that I can confirm "yes, I really am recording" without staring at a screen for cues that don't exist.**
  - **Acceptance Criteria:**
    - [ ] Running `record status` in the terminal prints a short, human-readable summary including:
      - whether the daemon is currently running,
      - whether the daemon is registered to auto-start at login,
      - whether the hotkey is currently active or is blocked because another running application has registered the same combination (see 2.13),
      - whether a capture is currently in progress, and
      - if a capture is in progress: how long it has been running and the absolute paths of the audio and (if applicable) video files being written.
    - [ ] `record status` exits with a zero exit code if the daemon is running and a non-zero exit code if it is not, so scripts can rely on it.

### 2.5 The global hotkey starts and stops capture

- **As the user, I want a single global keyboard shortcut to both start and stop a capture, so that recording a meeting takes one keypress at the beginning and one keypress at the end — no matter what app I'm in.**
  - **Acceptance Criteria:**
    - [ ] The hotkey works from any app, including while a meeting client (Zoom, Google Meet, Microsoft Teams) is in the foreground — the daemon receives the keypress before the meeting client sees it.
    - [ ] When no capture is in progress, pressing the hotkey starts a new capture. The session that starts is the same audio + video session defined in specs `001-mixed-mic-system-audio-capture` and `002-primary-display-video-capture` — a mixed mic + system audio `.wav` plus a primary-display `.mp4`.
    - [ ] When a capture is in progress, pressing the same hotkey ends that capture cleanly, finalizing both files (same behavior as `record stop`).
    - [ ] The hotkey is the same combination for start and stop — the user does not need two shortcuts.
    - [ ] Pressing the hotkey again while a capture is still in the middle of starting up, or in the middle of being finalized, does not begin a second capture. The daemon enforces "exactly one capture at a time" regardless of how fast the user presses the hotkey.
    - [ ] The hotkey leaves no trace inside the meeting: no extra participant appears in the meeting roster, no "recording" banner is triggered in the meeting client, and the keypress itself is not delivered to the meeting client.

### 2.6 Default hotkey and configurability

- **As the user, I want a sensible default hotkey out of the box, and I want to change it if it clashes with something I already use.**
  - **Acceptance Criteria:**
    - [ ] The default hotkey is **⌥⌘R** (Option + Command + R).
    - [ ] The user can change the hotkey by editing a configuration file (see 2.10). Supported hotkeys are any combination of one or more modifier keys (Command, Option, Control, Shift) plus a single non-modifier key.
    - [ ] After the user edits the configuration file, the new hotkey takes effect the next time the daemon starts (e.g., via `record daemon restart` or at the next login). The user is not required to restart their machine.
    - [ ] If the configuration file is missing or does not specify a hotkey, the default is used.
    - [ ] If the configuration file specifies an invalid or unparseable hotkey, the daemon logs a clear warning naming the problem and falls back to the default hotkey — it never refuses to start because of a bad hotkey value.

### 2.7 The terminal `record start` / `record stop` commands remain available

- **As the user, I want to be able to start and stop a capture from the terminal even though the hotkey is the everyday path, so that I can script captures or recover when the hotkey isn't behaving.**
  - **Acceptance Criteria:**
    - [ ] When the daemon is running, `record start` in the terminal triggers the **same** capture session as a hotkey press would, and `record stop` ends the running capture the same way a hotkey press would. There is one capture, regardless of which input channel started or stopped it.
    - [ ] Mixing input channels behaves consistently: a capture started by the hotkey can be stopped by `record stop`, and a capture started by `record start` can be stopped by the hotkey.
    - [ ] If the daemon is **not** running, `record start` and `record stop` print a clear message ("daemon is not running — try `record daemon start` or `record install`") and exit with a non-zero exit code. They do not silently spawn a one-off capture.

### 2.8 Audible feedback on start, stop, and error

- **As the user, I want a short, distinct sound at the moment a capture starts, a different short sound at the moment it stops, and a clearly different sound if something goes wrong, so that I can record a meeting confidently without ever switching to a terminal to check what happened.**
  - **Acceptance Criteria:**
    - [ ] When a capture **successfully starts**, the daemon plays one short macOS built-in system sound (e.g., the "Tink" sound).
    - [ ] When a capture **successfully stops**, the daemon plays a different short macOS built-in system sound (e.g., the "Pop" sound) — clearly distinguishable from the start sound by ear.
    - [ ] When the hotkey is pressed but a capture **cannot start** (e.g., a required permission is denied, the audio subsystem cannot attach, the daemon is in the middle of stopping a previous capture), the daemon plays a third, clearly distinct macOS built-in system sound (e.g., the "Funk" or "Basso" sound) and shows a macOS notification banner naming the specific problem (for example, "Microphone permission denied — grant in System Settings → Privacy & Security → Microphone").
    - [ ] All three sounds are short (well under one second) so they don't intrude on the start of a call.

### 2.9 The user can turn audible feedback off

- **As the user, I want to silence the start/stop/error sounds entirely when I'm in a quiet office or when I want zero audible cue at all.**
  - **Acceptance Criteria:**
    - [ ] The configuration file (see 2.10) exposes a setting that turns audible feedback on or off. The default is **on**.
    - [ ] When audible feedback is off, the daemon plays **no** sound on start, stop, or error.
    - [ ] When audible feedback is off, error situations still surface to the user via a macOS notification banner naming the specific problem. The banner is unaffected by the audible-feedback setting.
    - [ ] Changing the audible-feedback setting takes effect the next time the daemon starts.

### 2.10 Configuration file

- **As the user, I want a single, easy-to-find file where I can adjust the few settings this product exposes, so that I am not hunting through menus or commands.**
  - **Acceptance Criteria:**
    - [ ] The configuration file lives at `~/.config/record/config.toml`.
    - [ ] If the file does not exist, the daemon runs entirely with defaults; no error.
    - [ ] The file exposes exactly four settings in v1:
      1. **`hotkey`** — the keyboard shortcut to start/stop a capture. Default: `Option+Command+R`.
      2. **`output_folder`** — the absolute path of the folder where audio and video files are written. Default: `~/record/`. The folder is automatically created on first use if it does not exist.
      3. **`log_folder`** — the absolute path of the folder where the daemon writes its log file (see 2.14). Default: `~/record/logs/`. The folder is automatically created on daemon start if it does not exist. This setting is independent of `output_folder`; changing one does not move the other.
      4. **`audible_feedback`** — whether the start/stop/error sounds are played. Default: on.
    - [ ] Any other key found in the file is ignored with a logged warning (so future versions can add settings without breaking older daemons).
    - [ ] After the user edits the file, the new values take effect the next time the daemon starts.

### 2.11 Output folder for daemon-triggered captures

- **As the user, I want hotkey-triggered captures to land in a single predictable folder, so that I never have to wonder where today's recording went.**
  - **Acceptance Criteria:**
    - [ ] When a capture is started by hotkey (or by `record start` while the daemon is running), the resulting `.wav` and `.mp4` are written to the `output_folder` from the configuration file. The placeholder "current working directory" behavior from specs 001 and 002 does **not** apply when the daemon is in charge of a capture.
    - [ ] The default `output_folder` is `~/record/`, auto-created on first capture if it does not already exist.
    - [ ] The filename stem (timestamp-based, shared between the `.wav` and `.mp4`) follows the same convention defined in specs 001 and 002.
    - [ ] _Note: a future, separate specification ("local folder output with predictable naming") will further refine the naming convention and folder layout across the product. The behavior in this spec is the daemon's interim contract._

### 2.12 macOS permissions required by the daemon

- **As the user, I want macOS to ask me clearly for any permission the daemon needs, so that nothing fails silently and so that I'm in control of what the daemon can do.**
  - **Acceptance Criteria:**
    - [ ] The first time the daemon starts and needs the permission required to listen for a system-wide keyboard shortcut, macOS shows its standard permission dialog (Accessibility, or whichever macOS permission applies to global hotkeys). The daemon waits for the user's response.
    - [ ] If the user grants the permission, the daemon registers the hotkey and proceeds.
    - [ ] If the user denies the permission (now or later, by revoking it in System Settings), the daemon continues running but does **not** register the hotkey. It logs a clear warning naming exactly which permission is missing and where to grant it (System Settings → Privacy & Security). `record status` reflects "daemon running, hotkey disabled — permission missing." Captures can still be started by `record start` in the terminal.
    - [ ] The microphone, system-audio, and screen-recording permission behavior defined in specs 001 and 002 is unchanged — those prompts continue to appear the first time the daemon actually needs them (i.e., the first time a capture is started).

### 2.13 What happens when the hotkey is already taken by another app

- **As the user, I want the daemon to coexist peacefully with whatever other tools already use my chosen hotkey, even if that means the hotkey "doesn't work" for this product until I fix the conflict.**
  - **Acceptance Criteria:**
    - [ ] If another application has already registered the same hotkey when the daemon starts, the daemon does **not** display a popup, dialog, or notification about it. It logs the conflict in the daemon log and starts normally.
    - [ ] In that situation, the first application to have registered the shortcut continues to receive it — the user's keypress goes to that app, not to the daemon.
    - [ ] `record status` surfaces this state explicitly with a line such as "hotkey ⌥⌘R may be inactive — another application has registered the same combination," so the user can discover the conflict without reading the log.
    - [ ] The daemon continues to function in every other way: terminal `record start` / `record stop` still work, and the rest of `record status` still reports normally. The user can fix the conflict (quit the other app, change one of the two shortcuts) and then `record daemon restart` to retry.

### 2.14 Live feedback while the daemon and a capture are running

- **As the user, I want to be able to read a verbose log of what the daemon is doing, so that when something feels off I can see exactly what happened.**
  - **Acceptance Criteria:**
    - [ ] The daemon maintains a verbose, timestamped log of its events, including: daemon startup, hotkey registration outcome, configuration loaded (with the resolved hotkey, output folder, and log folder), each hotkey press received, capture start/stop events (delegating into the events defined in specs 001 and 002 once a capture is running), and daemon shutdown.
    - [ ] The log is plain text and is written to a file inside the configured `log_folder` (see 2.10). The default location is `~/record/logs/daemon.log`. The log folder is created automatically on daemon start if it does not exist.
    - [ ] The terminal logging behavior of specs 001 and 002 (events visible in the terminal while `record start` runs in the foreground) does not apply to daemon-triggered captures, since the daemon has no foreground terminal. The daemon's log file replaces the terminal output for daemon-triggered captures.

### 2.15 Capture session behavior is inherited from specs 001 and 002

- **As the user, I want the recording itself to behave exactly as it did when I was driving it from the terminal, so that "moving to a hotkey" doesn't change anything about the resulting files.**
  - **Acceptance Criteria:**
    - [ ] All capture-side behavior defined in `001-mixed-mic-system-audio-capture` and `002-primary-display-video-capture` applies unchanged when the capture is started by hotkey: a single mixed `.wav`, a primary-display `.mp4`, paired filenames, microphone permission and screen-recording permission prompts, mid-capture resilience, the "audio required, video best-effort" rule, the "screen lock / display sleep / system sleep ends the capture cleanly" rule, and the "invisible to the meeting" rule.
    - [ ] The only behaviors this spec overrides are: (a) the output folder is now the configured folder, not the current working directory, and (b) verbose event output goes to the daemon log file, not the foreground terminal.

---

## 3. Scope and Boundaries

### In-Scope

- A background daemon, running per-user, that registers itself to start automatically on macOS login via `record install` and can be removed via `record uninstall`.
- Manual lifecycle control of the running daemon via `record daemon start`, `record daemon stop`, `record daemon restart`.
- A `record status` command that reports daemon and capture state with appropriate exit codes, and surfaces hotkey-conflict state explicitly when another app holds the combination.
- A single global hotkey (default **⌥⌘R**) that toggles a capture session on and off from any app, leaving no trace inside the meeting client.
- User-editable configuration file at `~/.config/record/config.toml` exposing exactly four settings in v1: `hotkey`, `output_folder`, `log_folder`, `audible_feedback`. Defaults: `Option+Command+R`, `~/record/` (auto-created), `~/record/logs/` (auto-created), audible feedback on.
- Audible feedback using two distinct built-in macOS system sounds for capture-start and capture-stop, and a third built-in macOS system sound plus a notification banner for hotkey-triggered errors. A config switch to silence all sounds; errors continue to surface via the notification banner when sounds are off.
- Triggering the relevant macOS permission prompt on the daemon's first start to allow global-hotkey listening. Refusing to register the hotkey (but otherwise continuing to run) when that permission is denied.
- Quiet, first-come-first-served behavior on hotkey conflict with another application, surfaced in `record status` so the user can discover it without reading the log.
- Coexistence with the terminal `record start` / `record stop` commands from specs 001 and 002, both routed through the running daemon and operating on the same single capture session.
- A daemon log file (plain text, configurable folder, default `~/record/logs/daemon.log`) capturing all daemon and capture events in place of the foreground-terminal output used by specs 001 and 002 when the capture is driven by the daemon.
- All capture-side behavior (audio, video, filenames, permissions for mic/system audio/screen recording, mid-capture resilience, "audio required, video best-effort", screen-lock/sleep/lid-close cleanup, invisibility to the meeting) inherited unchanged from specs 001 and 002.

### Out-of-Scope

The following are explicitly NOT part of this specification. Each will be (or already is) addressed by its own specification or a later phase.

- **Any visible UI for the daemon** — menu bar icon, Dock icon, app window, settings GUI. The daemon has no UI in v1; configuration is via the config file only.
- **Multiple hotkeys** (separate start and stop combos, multiple captures bound to different combos). One toggling hotkey only.
- **Multiple simultaneous captures.** Exactly one capture at a time, regardless of input channel.
- **A GUI or interactive prompt for picking the hotkey, the output folder, or the log folder.** All three are config-file values.
- **Pausing and resuming a capture from the hotkey.** Press = start; press while running = stop. No pause.
- **Auto-resuming a capture after wake/unlock** following a screen-lock / display-sleep / system-sleep cleanup. (Inherited from spec 002; explicitly out here too.)
- **Loud popups or interactive prompts on hotkey conflict** — the daemon never interrupts the user about another app holding the same combo. The only surfaces for that state are `record status` and the log.
- **Log rotation, log retention policies, or a `record logs` viewer command.** The log is a single plain-text file in the configured `log_folder`; managing its size or history is out-of-scope for v1.
- **Local folder output with predictable naming** as a finished feature — this spec only defines the daemon's interim output-folder behavior (`output_folder` config + `~/record/` default). The full naming/layout spec ships separately in Phase 1.
- **Cloud-based transcription with speaker diarization** and **speaker-attributed transcript file.** (Separate Phase 1 specs.)
- **Meeting auto-detection (Zoom / Meet / Teams)**, **user-in-the-loop confirmation on auto-detect**, **generic "any audio call" detection**, **active meeting window video capture.** (Phase 2.)
- **Mic and system audio as separate tracks**, **voice-based diarization**, **per-speaker transcript tracks**, **live transcript stream**, **Phase 3 quality research spikes**, **active-speaker cue from video.** (Phase 3.)
- **App-managed library**, **transcript search**, **browser/replayer UI.** (Phase 4.)
- **On-device transcription and on-device diarization.** (Phase 5.)
- **Any mechanism that relies on a meeting platform's official recording API or in-meeting bot/extension presence.**
