# Functional Specification: Speaker-Attributed Transcript File (Cloud)

- **Roadmap Item:** Speaker-attributed transcript file (cloud) — After the call ends, send the audio to a cloud transcription provider with speaker diarization and write the resulting transcript (with timestamps and speaker labels) to the same local folder alongside the audio and video.
- **Status:** Draft
- **Author:** e

---

## 1. Overview and Rationale (The "Why")

Today, when a user finishes a call, they walk away with only an audio and video recording. To draft follow-ups or capture commitments, they have to re-listen to the recording — slow, and easy to miss things.

This change delivers a **written, speaker-attributed transcript automatically after every call**, placed right next to the recording. The user can skim it instead of re-listening: see who said what, and when.

After a capture session ends, the recorded audio is sent to a cloud transcription service that separates out the different voices in the conversation. The returned transcript is written into the same local folder as that session's audio and video.

In this version the audio is transcribed by a cloud service, which means **the audio leaves the user's machine to be transcribed** — a deliberate, known trade-off for v1, with on-device transcription planned later.

**Success is measured by:** the transcript appears reliably alongside the recording after each call, and is accurate enough to skim instead of re-listening.

---

## 2. Functional Requirements (The "What")

- **As a user, I want the transcript to be generated automatically when my call ends, so that I don't have to remember an extra step.**
  - **Acceptance Criteria:**
    - [ ] When a capture session stops, transcription begins on its own — no hotkey, click, or command needed.
    - [ ] The audio and video files are saved to the folder first, and remain there regardless of what happens with transcription.

- **As a user, I want the transcript delivered to the same folder as my recording, so that I can find it without hunting.**
  - **Acceptance Criteria:**
    - [ ] When transcription finishes, the transcript file(s) appear in the same user-configured folder as that session's audio and video.
    - [ ] The transcript file(s) share the same timestamp-based name as the recording, so it's obvious which session they belong to.
    - [ ] There is no notification on success — the file simply appears in the folder.

- **As a user, I want the transcript in a form I can both read and play along with, so that it fits how I work.**
  - **Acceptance Criteria:**
    - [ ] A **readable transcript** is produced: each entry shows the speaker label, a timestamp, and what was said — easy to skim top to bottom.
    - [ ] A **subtitle-style file** is also produced: timestamped caption segments that can be played alongside the video.
    - [ ] Both files are written for every session.

- **As a user, I want to see which distinct voices are speaking, so that the transcript is easy to follow.**
  - **Acceptance Criteria:**
    - [ ] Every distinct voice gets a generic, consistent label — "Speaker 1", "Speaker 2", and so on.
    - [ ] The same voice keeps the same label throughout a single transcript.
    - [ ] The user cannot rename speaker labels in this version.
    - [ ] Note: identifying which speaker is the user themselves is **not possible** in this version — the recording is a single mixed audio stream, so no voice can be reliably attributed to the user. This is deferred to the later "separate mic + system audio tracks" work.

- **As a user who speaks several languages, I want transcripts in the right language without setting it each call.**
  - **Acceptance Criteria:**
    - [ ] The spoken language of each recording is detected automatically — the user configures nothing and does nothing per call.
    - [ ] A recording that mixes languages is still transcribed, with each portion in the language actually spoken.

- **As a user running many calls a day, I want each recording transcribed on its own, so that finishing one call and starting another never blocks anything.**
  - **Acceptance Criteria:**
    - [ ] Multiple transcription jobs can be in progress at the same time.
    - [ ] Each transcript appears next to its own recording when it's ready, independently of the others.
    - [ ] Starting a new capture is never delayed by a transcription still in progress.

- **As a user, if a transcript can't be produced, I'd rather it fail quietly than get in my way — issues can be diagnosed afterward.**
  - **Acceptance Criteria:**
    - [ ] If transcription fails for any reason (no internet, the service is unavailable, the audio is rejected, or the saved access key is missing or invalid), nothing interrupts the user — no alert, no pop-up.
    - [ ] The transcript file simply does not appear next to the recording; the audio and video files remain in the folder.
    - [ ] The failure, and enough detail to diagnose it, is recorded in the logs.
    - [ ] There is no automatic retry.

- **As a user, I want to provide my transcription service access during install, so that it just works afterward.**
  - **Acceptance Criteria:**
    - [ ] During installation/setup, the user is asked for their cloud transcription service access key.
    - [ ] The access key is stored securely on the user's machine and used for all transcription from then on.
    - [ ] If the access key is ever missing when transcription tries to run, capture still works fully; transcription fails quietly and the reason is recorded in the logs.

---

## 3. Scope and Boundaries

### In-Scope

- Automatic transcription of a session's audio after capture ends, using a cloud service that separates speakers.
- Two transcript files per session — a readable transcript and a subtitle-style file — written to the same local folder, sharing the recording's name.
- Speaker labels: the user's own voice as "You", all other voices as consistent generic labels.
- Automatic language detection within the set of languages the user has configured.
- Independent, concurrent handling of multiple transcription jobs.
- Quiet failure with no automatic retry, with the reason recorded in the logs.
- Collecting the transcription service access key during install, storing it securely, and using it.

### Out-of-Scope

- Viewing or changing the stored transcription access key after install — handled separately.
- Automatic retry, or queuing failed transcriptions to retry later.
- Any user-facing notification, alert, or progress indication — for success or failure. The product has no notification surface; outcomes are observed via the file appearing (success) or the logs (failure).
- Renaming or editing speaker labels.
- Identifying which speaker is the user ("You" labelling) — not possible from a single mixed-audio recording; deferred to the later separate-tracks work.
- Configuring, restricting, or overriding the transcription language — it is always auto-detected.
- The Phase 1 capture work this depends on — hotkey daemon, video capture, mixed mic + system audio capture, and local folder naming — covered by their own specifications; this spec assumes the audio file already exists.
- On-device/local transcription (Phase 5).
- Live/streaming transcript during the call (Phase 3).
- Separate per-participant audio tracks and per-speaker transcript tracks (Phase 3).
- All other roadmap items: meeting auto-detection and active-meeting-window video capture (Phase 2), and the app-managed library, transcript search, and browser/replayer UI (Phase 4).
