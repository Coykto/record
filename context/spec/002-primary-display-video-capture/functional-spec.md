# Functional Specification: Primary-Display Video Capture

- **Roadmap Item:** Primary-display video capture (Phase 1 — Capture Foundation)
- **Status:** Draft
- **Author:** e

---

## 1. Overview and Rationale (The "Why")

The product's purpose is to give a privacy-conscious consultant a faithful record of every client call, captured entirely on their own machine. The first capture spec (`001-mixed-mic-system-audio-capture`) produces the audio side. This spec adds the video side: a screen recording of the meeting so the user can not only read the transcript afterward but also rewatch what was shown — slide decks, screen-shared demos, whiteboards — and tie spoken moments back to what was on screen.

The video covers the user's primary display. The user keeps the meeting visible on that display while capturing and walks away with a complete video record of what they saw.

Audio and video are produced by the **same** capture session. A single `record start` brings both up together; a single `record stop` finalizes both. This guarantees the audio and video are time-aligned and pairable on disk by their shared start timestamp.

**Success measure:** After a real meeting, the user has, in the same folder, an `.mp4` of their primary display and a `.wav` of the mixed mic + system audio for the exact same time range, both playable end-to-end, ready to feed downstream (transcription, archival, review).

---

## 2. Functional Requirements (The "What")

### 2.1 Starting and stopping a capture (audio AND video together)

This spec extends the start/stop behavior defined in `001-mixed-mic-system-audio-capture`. From this spec forward, `record start` and `record stop` operate on a single capture session that includes both audio and video.

- **As the user, I want a single command to start recording both my screen and my conversation, so that I don't have to coordinate two separate captures and worry about them drifting out of sync.**
  - **Acceptance Criteria:**
    - [ ] Running `record start` in the terminal begins a single capture session that records BOTH the primary display's video AND the mixed mic + system audio defined in the audio capture spec. The command returns control to the terminal once capture is healthy.
    - [ ] Running `record stop` ends the running capture, finalizes BOTH the video file and the audio file, and prints the final summary before returning.
    - [ ] If `record start` is run while a capture is already in progress, the command refuses to start a second capture, prints a clear message, and exits with a non-zero exit code (unchanged from the audio spec).
    - [ ] The stop summary names BOTH output files (absolute paths), the total recorded duration, and a description of which sources were actually captured end-to-end (for example, "microphone + system audio + primary display video" or "microphone + system audio; video unavailable").

### 2.2 What the video captures

- **As the user, I want the video to record what I see on my main screen so that the meeting content is preserved alongside the audio.**
  - **Acceptance Criteria:**
    - [ ] The video records the macOS **primary display** (the display containing the menu bar) — not any secondary display, not a specific window, not the full multi-monitor desktop.
    - [ ] The video is recorded at the primary display's **native resolution** and at **30 frames per second**.
    - [ ] The macOS **cursor is visible** in the recorded video at all times it is on the primary display.
    - [ ] The output is a single `.mp4` file per capture, containing the video stream only (no audio track). Audio is in the separate `.wav` file produced by the audio capture (see 2.3).
    - [ ] When both audio and video are captured for a session, the resulting `.mp4` and `.wav` start at the same moment and have the same duration (to within one video frame). The two files are time-aligned for the entire capture.

### 2.3 Where files are written

- **As the user, I want the audio and video files for one capture to land side by side with matching names, so that I can immediately tell which `.wav` and which `.mp4` go together.**
  - **Acceptance Criteria:**
    - [ ] Both files are written to the current working directory (the directory from which `record start` was run), per the placeholder behavior of the audio spec.
    - [ ] Both files share the same start-timestamp filename stem. Example pair: `2026-05-10T14-32-08.wav` and `2026-05-10T14-32-08.mp4`.
    - [ ] _Note: a future, separate specification ("local folder output with predictable naming") will replace this placeholder behavior with a user-configured output folder for both files._

### 2.4 Permissions on first run

- **As the user, I want macOS to ask me for screen-recording permission the first time I capture, so that I am not surprised by a silent failure and I am in control of what the app can access.**
  - **Acceptance Criteria:**
    - [ ] The first time `record start` runs and needs Screen Recording access, macOS shows its standard Screen Recording permission dialog. Capture waits for the user's response.
    - [ ] If the user grants the permission, video capture proceeds normally alongside audio.
    - [ ] If the user denies Screen Recording (now or in any later run, by revoking it in System Settings), `record start` prints a clear message naming exactly which permission is missing and where to grant it (System Settings → Privacy & Security → Screen Recording). It then continues per section 2.6 (audio is required, video is best-effort) — i.e., audio capture starts and the session proceeds without video; it does not fail the whole capture.
    - [ ] The microphone and system-audio permission behavior from the audio spec is unchanged.

### 2.5 Live feedback during capture

- **As the user, I want to see what the recording is doing while it runs, including the video side, so that I can confirm both audio and video are healthy without waiting for the meeting to end.**
  - **Acceptance Criteria:**
    - [ ] While a capture is running, the terminal continuously prints a verbose, timestamped log of capture events including (in addition to the audio events from the audio spec): video capture started, the primary display being recorded (name and resolution), periodic confirmations that video frames are flowing, any warnings, display-configuration changes (see 2.7), and video capture stopped.

### 2.6 Resilience: audio is required, video is best-effort

- **As the user, I want the audio to remain the primary product of every capture, with the video being a best-effort bonus, so that a video problem can never cost me the transcript-grade audio.**
  - **Acceptance Criteria:**
    - [ ] If video **fails to start** at the beginning of a capture (e.g., Screen Recording permission denied, no display detected), audio capture starts and the session proceeds without video. A clear warning is logged immediately. No `.mp4` is produced. The stop summary states "video unavailable" along with the reason.
    - [ ] If video **fails mid-capture** (e.g., screen capture crashes), audio capture continues to completion. The `.mp4` is finalized with whatever was captured up to the failure and is playable end-to-end. A warning is logged immediately and the stop summary names roughly when video stopped (for example, "video stopped at 02:14 — audio continued to end").
    - [ ] If audio **fails entirely** (no usable audio at all — neither microphone nor system audio is producing samples), the whole capture stops with a clear error, any partial video is finalized and saved, and the command exits non-zero. A capture is never finalized with video alone.
    - [ ] The per-source audio resilience defined in the audio spec is unchanged: losing one audio source mid-capture does not stop video.

### 2.7 Display configuration changes during capture

- **As the user, I want capture to keep running if I unplug a monitor, change resolution, or otherwise reconfigure my displays mid-meeting, so that I don't lose a recording to a routine hardware change.**
  - **Acceptance Criteria:**
    - [ ] If the primary display's resolution changes, the active display changes, or an external monitor is unplugged or plugged in during capture, video capture continues against the (possibly new) primary display.
    - [ ] Each such change is logged with a timestamp and a short description (for example, "display reconfigured at 14:32:08 — now recording primary display at 1920×1080").
    - [ ] The resulting `.mp4` is playable end-to-end. A change in resolution mid-file is acceptable; the file does not need to be re-encoded to a single uniform resolution.

### 2.8 Screen lock, display sleep, and system sleep all end the capture

- **As the user, I want any "I've stepped away" signal — screen lock, display sleep, or the system itself going to sleep — to end the capture cleanly and save the results, so that I never have to think about whether a recording is paused or running while I'm away from the Mac.**
  - **Acceptance Criteria:**
    - [ ] When the screen locks, the display goes to sleep, or the system enters full sleep (for example, the laptop lid is closed) during a capture, the capture is **ended cleanly** — as if `record stop` had been run at that moment.
    - [ ] Both the `.wav` and the `.mp4` (if any video was captured) are finalized and saved with whatever was captured up to the moment the event occurred. Both files are playable end-to-end.
    - [ ] The stop summary is logged and states clearly that the capture was ended by a system event (for example, "capture ended at 14:45:02 — system entered sleep" or "capture ended at 14:45:02 — screen locked").
    - [ ] After the machine wakes or is unlocked, no capture is automatically resumed. The user starts a new capture with `record start` if they want one.

### 2.9 Invisible to the meeting

- **As the user, I want the video capture to leave no trace inside the meeting itself, so that other participants are never notified and so that no client-side privacy banner appears.**
  - **Acceptance Criteria:**
    - [ ] No additional participant, bot, or attendee appears in the meeting roster while video capture is running.
    - [ ] No "recording" indicator is triggered inside the meeting client (Zoom, Google Meet, Microsoft Teams) by the screen capture.
    - [ ] Video capture relies on operating-system screen-recording facilities only — it does not interact with the meeting app's own recording features or screen-share controls in any way.

---

## 3. Scope and Boundaries

### In-Scope

- A single capture session, started by `record start` and ended by `record stop`, that records BOTH:
  - The macOS primary display's video, at native resolution and 30 fps, with the cursor visible, to a `.mp4` file (no audio track).
  - The mixed mic + system audio defined in `001-mixed-mic-system-audio-capture` (unchanged behavior), to a `.wav` file.
- Pairing audio and video files on disk via a shared start-timestamp filename stem in the current working directory, with matching duration and aligned start/end moments.
- Triggering the standard macOS Screen Recording permission dialog on first use; degrading to audio-only when Screen Recording is denied; refusing the whole capture only when audio is entirely unavailable.
- Verbose, timestamped, terminal-visible logging of video events (start, display info, periodic confirmations, warnings, display reconfiguration, stop) alongside the existing audio log.
- A final stop summary that names both file paths, the total duration, and which sources were actually captured.
- Continuing video capture across display configuration changes (resolution change, monitor unplug/plug, primary-display change).
- Ending the capture cleanly when the screen locks, the display sleeps, or the system enters sleep — finalizing both files. No auto-resume on wake.

### Out-of-Scope

The following are explicitly NOT part of this specification. Each will be (or already is) addressed by its own specification or a later phase.

- **Active meeting window video capture** — recording only the Zoom / Google Meet / Microsoft Teams window rather than the whole primary display. (Phase 2 — Precision Video Capture.)
- **Capturing secondary displays, all displays, or multi-monitor stitched output.** Phase 1 records the primary display only.
- **In-app selection of which display to record** (other than "primary").
- **Configurable frame rate, resolution, codec, or video bitrate** — Phase 1 is fixed at primary-display native resolution, 30 fps, `.mp4`.
- **A muxed `.mp4` containing both video and audio in one file.** Audio and video remain in two separate files (`.wav` + `.mp4`) sharing a timestamp.
- **Audio formats other than `.wav` or video formats other than `.mp4`.**
- **Post-processing of video** (cropping, scaling beyond native, watermarking, redaction, blurring).
- **Hiding the cursor**, redacting on-screen content, or any privacy-filtering of what appears on the primary display during capture. The user is responsible for what is visible on their primary display while capturing.
- **Pausing and resuming video mid-capture.** All "user stepped away" signals end the capture cleanly instead.
- **Auto-resuming capture when the machine wakes or is unlocked.**
- **Hotkey-triggered background daemon** — the global hotkey that will eventually replace `record start` / `record stop`. (Separate Phase 1 spec.)
- **Local folder output with predictable naming** — the user-configured output folder and finalized naming convention that will replace "current working directory + timestamp filename." (Separate Phase 1 spec.)
- **Cloud-based transcription with speaker diarization** and **speaker-attributed transcript file**. (Separate Phase 1 specs.)
- **Meeting auto-detection**, **user-in-the-loop confirmation**, **generic "any audio call" detection.** (Phase 2.)
- **Voice-based diarization**, **per-speaker transcript tracks**, **live transcript stream**, **Phase 3 quality research spikes**, **active-speaker cue from video.** (Phase 3.)
- **App-managed library**, **transcript search**, **browser/replayer UI.** (Phase 4.)
- **On-device transcription and on-device diarization.** (Phase 5.)
- **Any mechanism that relies on a meeting platform's official recording API or in-meeting bot/extension presence.**
