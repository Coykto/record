# Functional Specification: Mixed Mic + System Audio Capture

- **Roadmap Item:** Mixed mic + system audio capture (Phase 1 — Capture Foundation)
- **Status:** Draft
- **Author:** e

---

## 1. Overview and Rationale (The "Why")

The product's purpose is to give a privacy-conscious consultant a faithful record of every client call, captured entirely on their own machine, that can be turned into a speaker-attributed transcript afterward. To do that, we first need a single audio file containing both ends of the conversation: what the user said (microphone) and what the other participants said (system audio output from the meeting app).

Today, the user has no way to produce such a file without inviting a third-party meeting bot into the call, which is exactly the privacy problem the product exists to avoid. This specification defines the simplest reliable way to produce that file on macOS, with no presence inside the meeting at all.

This is the **first** capture capability in Phase 1 — there must be something to capture before there is anything to trigger or to organize on disk. The hotkey, the video-window capture, and the configured output folder are each separate specifications.

**Success measure:** After a real meeting, the user has a single audio file on disk containing both the user's voice and the remote participants' voices, of high enough quality to feed into a cloud transcription service and get a usable transcript back.

---

## 2. Functional Requirements (The "What")

### 2.1 Starting and stopping a capture

- **As the user, I want to start an audio capture from the terminal so that I can record a meeting in progress.**
  - **Acceptance Criteria:**
    - [ ] Running `record start` in the terminal begins a new audio capture and returns control to the terminal.
    - [ ] If `record start` is run while a capture is already running, the command refuses to start a second capture, prints a clear message stating that a capture is already in progress, and exits with a non-zero exit code.

- **As the user, I want to stop an audio capture from the terminal so that the recorded audio is finalized and saved to a file.**
  - **Acceptance Criteria:**
    - [ ] Running `record stop` in the terminal ends the running capture and writes the audio file to disk before returning.
    - [ ] When the capture stops, the terminal prints a final summary that includes the absolute path to the saved file, the total recorded duration, and a description of which audio sources were actually captured (for example, "microphone + system audio" or "system audio only — microphone dropped at 02:14").

### 2.2 What gets captured

- **As the user, I want both my own voice and what I hear from the meeting captured into a single audio file, so that a transcription service can read the entire conversation in one pass.**
  - **Acceptance Criteria:**
    - [ ] The output file contains a single audio stream that mixes the user's microphone input with the system's audio output.
    - [ ] The microphone source is whichever input device macOS currently has selected as the default input (System Settings → Sound → Input). The user does not pick a microphone in the application — switching the system default switches the captured microphone.
    - [ ] The system audio source is whatever audio is being played by other applications on the machine (typically the meeting app's audio).
    - [ ] The output file is in `.wav` format (uncompressed PCM). No other formats are produced.
    - [ ] The file is suitable as input to a standard cloud speech-to-text service with speaker diarization.

### 2.3 Where the file is written

- **As the user, I want the resulting file to land in a predictable, easy-to-find location, so that I can hand it to a transcription tool right away.**
  - **Acceptance Criteria:**
    - [ ] The audio file is written to the current working directory (the directory from which `record start` was run).
    - [ ] The filename is based on the capture's start timestamp (for example, `2026-05-10T14-32-08.wav`). The exact timestamp format is consistent across captures so files sort chronologically by name.
    - [ ] _Note: a future, separate specification ("local folder output with predictable naming") will replace this placeholder behavior with a user-configured output folder._

### 2.4 Permissions on first run

- **As the user, I want macOS to ask me for the recording permissions the first time I capture, so that I am not surprised by a silent failure and so that I am in control of what the app can access.**
  - **Acceptance Criteria:**
    - [ ] The first time `record start` runs and needs microphone access, macOS shows its standard microphone-permission dialog. Capture waits for the user's response.
    - [ ] The first time `record start` runs and needs system-audio access, macOS shows its standard system-level recording-permission dialog. Capture waits for the user's response.
    - [ ] If the user grants both permissions, capture proceeds normally.
    - [ ] If the user denies either permission (then or in any later run, by revoking it in System Settings), `record start` refuses to begin a capture, prints a clear message naming exactly which permission is missing and where to grant it (System Settings → Privacy & Security), and exits with a non-zero exit code.

### 2.5 Live feedback during capture

- **As the user, I want to see what the recording is doing while it runs, so that I can confirm capture is healthy without having to wait for the meeting to end.**
  - **Acceptance Criteria:**
    - [ ] While a capture is running, the terminal continuously prints a verbose log of capture events, including: capture started, each audio source successfully attached, periodic confirmations that audio is flowing, any warnings, and capture stopped.
    - [ ] Log lines are timestamped so the user can correlate them with what was happening in the meeting.

### 2.6 Resilience to mid-capture problems

- **As the user, I want the recording to keep going if one of the two audio sources fails partway through, so that I do not lose the rest of a long meeting because of a transient hardware glitch.**
  - **Acceptance Criteria:**
    - [ ] If the microphone becomes unavailable during a capture (for example, AirPods disconnect, USB mic is unplugged), the capture continues with system audio only. A warning is logged immediately when the failure occurs.
    - [ ] If system audio becomes unavailable during a capture, the capture continues with the microphone only. A warning is logged immediately when the failure occurs.
    - [ ] If a source is lost mid-capture, the final stop summary clearly states what was lost and roughly when (for example, "microphone dropped at 02:14 — remainder is system audio only").
    - [ ] The resulting audio file is still produced and is playable end-to-end, even when one source died partway through.

### 2.7 Invisible to the meeting

- **As the user, I want the capture to leave no trace inside the meeting itself, so that other participants are never notified that I am recording and so that no client-side privacy banner appears.**
  - **Acceptance Criteria:**
    - [ ] No additional participant, bot, or attendee appears in the meeting roster while capture is running.
    - [ ] No "recording" indicator is triggered inside the meeting client (Zoom, Google Meet, Microsoft Teams) by the capture.
    - [ ] Capture relies on operating-system audio facilities only — it does not interact with the meeting app's own recording features in any way.

---

## 3. Scope and Boundaries

### In-Scope

- Capturing the user's microphone (whichever device is the macOS system default input) together with the system's audio output, mixed into a single audio stream.
- Producing a single `.wav` (uncompressed PCM) file per capture.
- Writing that file to the current working directory with a timestamp-based filename.
- Starting and stopping captures via `record start` and `record stop` in the terminal.
- Triggering the standard macOS microphone and system-audio permission dialogs on first use; refusing to capture cleanly when permissions are denied.
- Verbose, timestamped, terminal-visible logging of capture events while a capture is running.
- A final summary on stop that includes path, duration, and which sources were actually captured.
- Continuing capture with whatever sources remain available if one source fails mid-recording.
- Refusing to start a second concurrent capture and exiting with a clear error.

### Out-of-Scope

The following are explicitly NOT part of this specification. Each will be (or already is) addressed by its own specification:

- **Hotkey-triggered background daemon** — the global hotkey that will eventually replace the `record start` / `record stop` commands. (Separate Phase 1 spec.)
- **Active meeting window video capture** — recording the meeting app's window to `.mp4`. (Separate Phase 1 spec.)
- **Local folder output with predictable naming** — the user-configured output folder and finalized naming convention that will replace the "current working directory + timestamp filename" placeholder used here. (Separate Phase 1 spec.)
- **Cloud-based transcription with speaker diarization** and **speaker-attributed transcript file** — the post-capture transcription pipeline. (Separate Phase 1 specs.)
- **Meeting auto-detection**, **user-in-the-loop confirmation**, **generic "any audio call" detection**. (Phase 2.)
- **Mic and system audio as separate tracks**, **voice-based diarization**, **per-speaker transcript tracks**, **live transcript stream**, and the Phase 3 quality research spikes.
- **App-managed library**, **transcript search**, **browser/replayer UI**. (Phase 4.)
- **On-device transcription and on-device diarization.** (Phase 5.)
- **Audio formats other than `.wav`** (e.g., `.m4a`, `.mp3`).
- **In-app selection of a specific microphone** other than the macOS system default.
- **Audio post-processing** (noise reduction, normalization, level matching between mic and system audio) beyond what's needed to produce a playable mixed file.
- **Any mechanism that relies on a meeting platform's official recording API or in-meeting bot/extension presence.**
