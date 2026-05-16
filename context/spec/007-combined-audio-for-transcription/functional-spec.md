# Functional Specification: Combined Mic + System Audio File for Transcription

- **Roadmap Item:** Re-introduces the Phase 1 "Mixed mic + system audio capture" deliverable as a **derived artifact** alongside the two independent source files (which remain — see spec 005). Updates the Phase 1 "Speaker-attributed transcript file (cloud)" deliverable (see spec 004) to send a single combined audio file to the cloud transcription service instead of the source files.
- **Status:** Draft
- **Author:** e

---

## 1. Overview and Rationale (The "Why")

Today, every capture session produces two independent audio files — one for the user's microphone and one for the system's audio output. That separation is valuable for debugging (the user can play each side in isolation and confirm exactly what each source captured) and for any future per-source processing, so it must be preserved.

But the **cloud transcription service expects a single audio file per session**, not two. Sending the two source files separately is awkward (which transcript belongs to which?), doubles transcription cost, and produces two disjoint speaker-attributed transcripts that the user then has to mentally interleave — exactly the kind of post-call friction the product is meant to eliminate.

This change adds, at the moment a capture stops, a **third audio file** for the session: a single combined recording of the user's voice and the meeting's audio, mixed together in real time so a transcription service can produce one coherent transcript of the whole conversation. The two source files are left on disk untouched, so the debug-and-diagnose benefit of spec 005 is fully preserved. The combined file becomes the artifact that is handed to the cloud transcription service.

**Success measure:** After a real meeting, the user's session folder contains three audio files — the mic source, the system-audio source, and a combined file — and the transcript that appears alongside them is generated from the combined file and covers the entire conversation (both sides) end to end.

---

## 2. Functional Requirements (The "What")

### 2.1 A combined audio file is produced at the end of every capture

- **As the user, I want one combined audio file produced automatically when I stop a capture, so that the entire conversation (my voice plus everything I heard) lives in a single file I can hand to a transcription service.**
  - **Acceptance Criteria:**
    - [ ] Every successful capture session produces a combined audio file in addition to the existing two source files.
    - [ ] The combined file is produced by mixing the two source files of that session, without re-recording.
    - [ ] The user does not run an extra command or pass any extra flag — the combined file is produced as part of stopping the capture.

### 2.2 Naming and location of the combined file

- **As the user, I want the combined file to have an obvious, primary-feeling name that pairs it with the session's source files, so that I can tell at a glance which file is "the recording" and which two are the per-source originals.**
  - **Acceptance Criteria:**
    - [ ] The combined file is written to the same folder as that session's two source files.
    - [ ] The combined file is named `<timestamp>.wav`, using the same timestamp as the session's source files (which keep their `<timestamp>-mic.wav` and `<timestamp>-system.wav` names).
    - [ ] All three files for a session sort next to each other alphabetically in the folder.
    - [ ] The combined file is `.wav` (uncompressed PCM), matching the format of the source files.

### 2.3 What the combined file contains

- **As the user, I want the combined file to contain my voice and the meeting's audio at the same time, so that the transcription service hears the conversation as it actually happened.**
  - **Acceptance Criteria:**
    - [ ] The combined file is a single-channel (mono) recording.
    - [ ] At any moment in time, the combined file contains the sum of the microphone source and the system-audio source as they were at that same moment in the capture.
    - [ ] Both source contributions are present at equal levels (no boosting or attenuation of one side relative to the other).
    - [ ] The combined file starts at the same moment in time as the two source files, so that the three files line up when played from their start.

### 2.4 Source files are preserved

- **As the user, I want the two source files to remain on disk exactly as they are today after the combined file is produced, so that I can still inspect each source individually for debugging.**
  - **Acceptance Criteria:**
    - [ ] After a capture stops, the session's `<timestamp>-mic.wav` and `<timestamp>-system.wav` files are present on disk and are identical to what they would have been without this change.
    - [ ] Producing the combined file never modifies, moves, or deletes the source files.
    - [ ] If the combined file fails to be produced, the two source files are still on disk and intact.

### 2.5 Stop waits until the combined file is on disk

- **As the user, I want `record stop` to come back to me only after the combined file exists on disk, so that when the command returns I know the session is fully finalized.**
  - **Acceptance Criteria:**
    - [ ] Running `record stop` does not return control to the terminal until the combined file has been finalized on disk (or has been determined to have failed; see 2.8).
    - [ ] The stop command produces no output suggesting it is done until the combined file outcome is settled.

### 2.6 Mismatched source durations

- **As the user, I want the combined file to cover the entire meeting even when one of the two sources dropped out partway through, so that no part of the conversation is missing from the transcript.**
  - **Acceptance Criteria:**
    - [ ] When the two source files have different durations (for example, the mic dropped at 02:14 but the system audio recorded for the full 45 minutes), the combined file has the duration of the **longer** source.
    - [ ] During the stretch where one source has ended, the combined file plays back only the remaining source (the absent side is silent).
    - [ ] The transition from "both sources present" to "only one source present" is seamless to listen to — no clicks, no abrupt level changes beyond the natural loss of the absent source.
    - [ ] If both source files are entirely silent, the combined file is still produced as a valid, playable, silent `.wav` of the session's duration.

### 2.7 Stop summary now includes the combined file

- **As the user, I want the existing stop summary to also tell me, in one extra line, whether the combined file was produced, so that I can confirm at a glance that the session is fully usable for transcription.**
  - **Acceptance Criteria:**
    - [ ] The terminal summary printed when `record stop` finishes continues to show, for the mic and system-audio files, exactly the per-file detail it shows today (absolute path, duration, one-line status such as "captured normally", "silent throughout", or "microphone dropped at 02:14 — file ends there").
    - [ ] One additional line is added to the summary that names the combined file by its absolute path and states either that it was produced (with its duration) or that producing it failed (with a brief reason in plain language).

### 2.8 Quiet failure when the combined file cannot be produced

- **As the user, I would rather have my two source files preserved and skip transcription than have the system block on a failed combine step, so that a problem at stop time never costs me my recording.**
  - **Acceptance Criteria:**
    - [ ] If producing the combined file fails for any reason (for example, the disk is full, one of the source files cannot be read, or an unexpected error occurs), no alert or pop-up is shown.
    - [ ] When this happens, both source files remain on disk and intact, the combined file is not present in the folder, and transcription is **not** attempted for that session.
    - [ ] The reason for the failure, with enough detail to diagnose it, is recorded in the logs.
    - [ ] The stop summary's extra line for the combined file states clearly that it was not produced, in plain language understandable by a non-developer.

### 2.9 The combined file is what gets transcribed

- **As the user, I want only the combined file sent to the cloud transcription service, so that I receive a single, coherent transcript covering the whole conversation rather than two disjoint ones.**
  - **Acceptance Criteria:**
    - [ ] When a capture session ends successfully and the combined file has been produced, the transcription step (see spec 004) is run against the combined file only.
    - [ ] The two source files are never sent to the cloud transcription service.
    - [ ] The resulting transcript file(s) appear next to the recording in the session folder, using the session's timestamp as their name (as already specified in spec 004).
    - [ ] If the combined file was not produced (per 2.8), no transcription is attempted, no transcript file appears, and the source files remain on disk.

### 2.10 Live feedback during capture is unchanged

- **As the user, I want the in-call experience to feel exactly the same as it does today, so that this change adds work only at the very end of a session.**
  - **Acceptance Criteria:**
    - [ ] While a capture is running, the terminal log output describing the two source streams is identical to today's behavior (per spec 005, section 2.6).
    - [ ] No new during-capture log lines about combining are produced; combining is a stop-time activity only.

---

## 3. Scope and Boundaries

### In-Scope

- Producing **one additional `.wav` file per capture session**, named `<timestamp>.wav`, in the same folder as the session's two source files.
- The combined file is a **mono mix** of the two source files at **equal levels**, sharing the session's start moment.
- The combined file's duration equals the **longer** of the two source files; stretches where one source has ended are filled with silence on the absent side.
- A silent-but-valid combined file is produced when both source files are silent.
- `record stop` blocks until the combined file is on disk (or has been determined to have failed).
- The stop summary keeps its existing per-source detail and gains **one extra line** describing the combined file's outcome.
- The two source files are preserved exactly as they are today; they are never modified, moved, or deleted by this work.
- Cloud transcription (per spec 004) now receives **only** the combined file.
- Failure to produce the combined file is handled **quietly**: source files are kept, transcription is skipped, the failure is logged, and the stop summary states the combined file was not produced.

### Out-of-Scope

- **Changing the two existing source files in any way.** Their format, contents, naming, and behavior (per spec 005) are unchanged.
- **Removing the source files after the combined file is produced.** They stay on disk as the canonical per-source originals (explicit user requirement — useful for debugging).
- **Audio formats other than `.wav`** for the combined file (no `.m4a`, `.mp3`, etc.).
- **Stereo combined files** (mic on one channel, system on the other) and any other non-mono channel layout.
- **Level adjustment, normalization, noise reduction, or any other audio post-processing** of either source before mixing, beyond what is needed to produce a playable mono mix.
- **Automatic retry of a failed combine step.** A single attempt is made per capture stop; if it fails, the session is left with its two source files and no transcript.
- **User-facing notifications, alerts, or progress indicators** about the combine step, success or failure. The terminal stop summary line is the only surface.
- **Sample-accurate cross-file synchronization guarantees** between the combined file and the source files, beyond sharing a start moment (carried over from spec 005's same limitation).
- **Sending the source files to transcription as a fallback** when the combined file fails. The chosen behavior is to skip transcription entirely in that case.
- **In-call (during-capture) combining or live transcription.** Combining happens only at stop.
- **All other roadmap items**, which are addressed by their own specifications: hotkey-triggered background daemon, primary-display video capture, active meeting window video capture, local folder output with predictable naming, meeting auto-detection and user-in-the-loop control, voice-based diarization on the system-audio stream, per-speaker transcript tracks, live transcript stream, ML-based source separation, active-speaker cue from video, app-managed library, transcript search, browser/replayer UI, on-device transcription, and on-device speaker diarization.
