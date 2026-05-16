# Functional Specification: Independent Mic and System Audio WAV Files

- **Roadmap Item:** Mic + system audio as separate tracks (pulled from Phase 3 into Phase 1 — supersedes the Phase 1 "Mixed mic + system audio capture" item)
- **Status:** Draft
- **Author:** e

---

## 1. Overview and Rationale (The "Why")

The product's purpose is to give a privacy-conscious consultant a faithful record of every client call — captured entirely on their own machine — that can be turned into a speaker-attributed transcript afterward. The consultant's single biggest frustration with existing tools is that mixed-audio transcripts mangle the moments where two people talk over each other, and those are exactly the moments that matter (commitments, clarifications, push-back).

The earlier draft of this capability produced a single mixed audio file. That choice made overlapping speech permanently fused: once two voices are summed into one waveform, no downstream tool can cleanly pull them apart. By writing the user's microphone and the system's audio output as **two independent files in the same capture session**, we get a hard, mechanical separation of "what the user said" from "what everyone else said" — for free, with no machine learning, no probabilistic guessing, no quality regressions on noisy calls. Downstream transcription and diarization keep that separation and can produce a transcript where overlapping moments are preserved verbatim instead of collapsed into one garbled passage.

This specification **fully replaces** the previous mixed-audio behavior. A capture session no longer produces a mixed file; it produces exactly two files, one per source. Everything else about the capture experience (how a session starts and stops, where files land, permission prompts, live feedback) carries over from the previous spec with only the file-output details changed.

**Success measure:** After a real meeting, the user has two audio files on disk — one containing only their own voice, one containing only what the meeting played through their speakers — both of high enough quality to feed to a cloud transcription/diarization service and recover an accurate, speaker-attributed transcript, including over overlapping segments.

---

## 2. Functional Requirements (The "What")

### 2.1 Starting and stopping a capture

- **As the user, I want to start an audio capture from the terminal so that I can record a meeting in progress.**
  - **Acceptance Criteria:**
    - [ ] Running `record start` in the terminal begins a new audio capture and returns control to the terminal.
    - [ ] If `record start` is run while a capture is already running, the command refuses to start a second capture, prints a clear message stating that a capture is already in progress, and exits with a non-zero exit code.

- **As the user, I want to stop an audio capture from the terminal so that the recorded audio is finalized and saved as two files.**
  - **Acceptance Criteria:**
    - [ ] Running `record stop` in the terminal ends the running capture and finalizes both audio files on disk before returning.
    - [ ] When the capture stops, the terminal prints a final summary that lists, for each file: the absolute path, the recorded duration, and a one-line status (for example, "captured normally", "silent throughout", or "microphone dropped at 02:14 — file ends there").

### 2.2 What gets captured

- **As the user, I want my own voice and what I hear from the meeting written to two separate files in a single capture, so that a transcription service can keep "me" and "everyone else" distinct end-to-end.**
  - **Acceptance Criteria:**
    - [ ] A single capture session produces exactly two output files: one for the microphone source and one for the system-audio source.
    - [ ] The microphone file contains **only** what the microphone recorded. It contains no system audio, no echo of the system audio, and no mix.
    - [ ] The system-audio file contains **only** the audio being played by other applications on the machine (typically the meeting app's audio). It contains no microphone input and no mix.
    - [ ] The microphone source is whichever input device macOS currently has selected as the default input (System Settings → Sound → Input). The user does not pick a microphone in the application — switching the system default switches the captured microphone.
    - [ ] Both files are in `.wav` format (uncompressed PCM). No other formats are produced.
    - [ ] Both files are suitable as input to a standard cloud speech-to-text service with speaker diarization.

### 2.3 File naming and location

- **As the user, I want both files to land in a predictable, easy-to-find location with names that pair them as one session, so that I can find and hand off the right pair without confusion.**
  - **Acceptance Criteria:**
    - [ ] Both files are written to the current working directory (the directory from which `record start` was run).
    - [ ] Both filenames share a common basename derived from the capture's start timestamp (for example, `2026-05-15T14-32-08`). The two files in a single session differ only by their source suffix.
    - [ ] The microphone file is named `<timestamp>-mic.wav`.
    - [ ] The system-audio file is named `<timestamp>-system.wav`.
    - [ ] The timestamp format is consistent across captures so files from different sessions sort chronologically by name and each session's two files sit next to each other alphabetically.
    - [ ] _Note: a future, separate specification ("local folder output with predictable naming") will replace this placeholder behavior with a user-configured output folder._

### 2.4 Independence of the two files

- **As the user, I want each file to be produced by its own dedicated writer, so that a problem with one source can never silently corrupt or shorten the other file.**
  - **Acceptance Criteria:**
    - [ ] Each output file is fed by exactly one source. The microphone file never receives system audio and the system-audio file never receives microphone input — not as a mix, not as a fallback, not under any failure condition.
    - [ ] If one source produces no audio for the entire capture, the other file is unaffected: it records normally and contains exactly what its source produced.
    - [ ] If one source fails partway through a capture, the other file is unaffected: it continues recording up to the user's stop command, and its duration reflects the full session.
    - [ ] Both files share the same start moment so that, when both sources are working, they can be played back together and line up.

### 2.5 Permissions on first run

- **As the user, I want macOS to ask me for the recording permissions the first time I capture, and to be told clearly if any permission is missing, so that I am never surprised by a silent failure.**
  - **Acceptance Criteria:**
    - [ ] The first time `record start` runs and needs microphone access, macOS shows its standard microphone-permission dialog. Capture waits for the user's response.
    - [ ] The first time `record start` runs and needs system-audio access, macOS shows its standard system-level recording-permission dialog. Capture waits for the user's response.
    - [ ] If both permissions are granted, capture proceeds and both files are produced.
    - [ ] If **either** permission is denied (then or in any later run, by revoking it in System Settings), `record start` refuses to begin a capture, prints a clear message naming **which** permission is missing and where to grant it (System Settings → Privacy & Security), and exits with a non-zero exit code. A capture is never started with only one of the two sources available because of a permission gap.

### 2.6 Live feedback during capture

- **As the user, I want to see what each source is doing while the recording runs, so that I can spot at a glance if one side has stopped working without having to wait for the meeting to end.**
  - **Acceptance Criteria:**
    - [ ] While a capture is running, the terminal continuously prints a verbose log of capture events.
    - [ ] Every log line that refers to a source clearly identifies which source it belongs to (microphone or system audio), so that the two streams' events can be read independently in the output.
    - [ ] Logged events include, per source: source successfully attached, periodic confirmations that audio is flowing, warnings (for example, source went silent or unavailable), and source closed at stop.
    - [ ] Log lines are timestamped so the user can correlate them with what was happening in the meeting.

### 2.7 Silent source

- **As the user, I want a file to always be produced for each source, even if that source happened to be silent the whole time, so that my session output is predictable for downstream tools.**
  - **Acceptance Criteria:**
    - [ ] If a source produces no audio whatsoever during the capture (for example, the microphone was muted at the hardware level for the entire meeting), its file is still written to disk as a valid, playable `.wav` of the session's duration containing silence.
    - [ ] The final stop summary names that file and states clearly that the source was silent throughout, so the user is not misled into thinking the file is broken.

### 2.8 Resilience to mid-capture problems

- **As the user, I want the recording to keep going on the surviving source if one of the two sources fails partway through, so that I do not lose the rest of a long meeting because of a transient hardware glitch on the other side.**
  - **Acceptance Criteria:**
    - [ ] If the microphone becomes unavailable during a capture (for example, AirPods disconnect, USB mic is unplugged), the microphone file is finalized at the point of failure as a valid, playable `.wav`. A warning is logged immediately when the failure occurs. The system-audio file is unaffected and continues recording until the user stops the capture.
    - [ ] If system audio becomes unavailable during a capture, the system-audio file is finalized at the point of failure as a valid, playable `.wav`. A warning is logged immediately when the failure occurs. The microphone file is unaffected and continues recording until the user stops the capture.
    - [ ] If a source is lost mid-capture, the final stop summary clearly states which file was truncated and roughly when (for example, "microphone dropped at 02:14 — file ends there").
    - [ ] Both files are still produced (even if one is shorter than the other) and each one is playable end-to-end.

### 2.9 Invisible to the meeting

- **As the user, I want the capture to leave no trace inside the meeting itself, so that other participants are never notified that I am recording and so that no client-side privacy banner appears.**
  - **Acceptance Criteria:**
    - [ ] No additional participant, bot, or attendee appears in the meeting roster while capture is running.
    - [ ] No "recording" indicator is triggered inside the meeting client (Zoom, Google Meet, Microsoft Teams) by the capture.
    - [ ] Capture relies on operating-system audio facilities only — it does not interact with the meeting app's own recording features in any way.

---

## 3. Scope and Boundaries

### In-Scope

- Capturing the user's microphone (whichever device is the macOS system default input) and the system's audio output as **two independent audio streams** within a single capture session.
- Writing exactly **two `.wav` (uncompressed PCM) files per capture**: `<timestamp>-mic.wav` and `<timestamp>-system.wav`.
- Writing those files to the current working directory using a shared timestamp-based basename.
- Strict independence between the two writers: one source's failure or silence cannot affect the other file.
- Starting and stopping captures via `record start` and `record stop` in the terminal.
- Triggering the standard macOS microphone and system-audio permission dialogs on first use; refusing to capture cleanly if either permission is denied.
- Verbose, timestamped, terminal-visible logging during capture, with each log line clearly tagged by source.
- A final stop summary listing both files, their durations, and a per-file status (normal, silent throughout, or truncated at time X).
- Producing a silent-but-valid `.wav` for any source that produced no audio during the session.
- Continuing capture on the surviving source if the other source fails mid-recording, and finalizing the failed source's file at the point of failure.
- Refusing to start a second concurrent capture and exiting with a clear error.

### Out-of-Scope

The following are explicitly NOT part of this specification. Each is (or will be) addressed by its own specification, or by an earlier/later phase of the product:

- **Producing a mixed mic + system audio file.** This spec fully replaces that behavior; no mixed file is produced.
- **Hotkey-triggered background daemon** — the global hotkey that will eventually replace the `record start` / `record stop` commands. (Separate Phase 1 spec.)
- **Primary-display video capture** and **active meeting window video capture**. (Separate Phase 1 / Phase 2 specs.)
- **Local folder output with predictable naming** — the user-configured output folder and finalized naming convention that will replace the "current working directory + timestamp basename" placeholder used here. (Separate Phase 1 spec.)
- **Cloud-based transcription with speaker diarization** and **speaker-attributed transcript file**. (Separate Phase 1 specs.)
- **Meeting auto-detection**, **user-in-the-loop confirmation**, **generic "any audio call" detection**. (Phase 2.)
- **Voice-based diarization on the system-audio stream**, **per-speaker transcript tracks**, **live transcript stream**, and the Phase 3 quality research spikes.
- **App-managed library**, **transcript search**, **browser/replayer UI**. (Phase 4.)
- **On-device transcription and on-device diarization.** (Phase 5.)
- **Audio formats other than `.wav`** (for example, `.m4a`, `.mp3`).
- **In-app selection of a specific microphone** other than the macOS system default.
- **Audio post-processing** (noise reduction, normalization, level matching between mic and system audio) beyond what's needed to produce playable files.
- **Sample-accurate cross-file synchronization guarantees.** The two files share a start moment and are intended to line up when played together; bit-exact, sample-aligned sync between them is not promised.
- **In-app selection of an output device or routing.**
- **Any mechanism that relies on a meeting platform's official recording API or in-meeting bot/extension presence.**
