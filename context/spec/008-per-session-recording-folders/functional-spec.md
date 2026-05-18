# Functional Specification: Per-Session Recording Folders with a Human-Readable Name

- **Roadmap Item:** Phase 1 — "Local folder output with predictable naming: Write video and audio to a user-configured local folder using a stable naming convention (timestamp-based) so artifacts are easy to find without an in-app library."
- **Status:** Draft
- **Author:** e

---

## 1. Overview and Rationale (The "Why")

Today, every capture session writes its files (the two source audio files, the combined audio file, and — once it returns — the transcript) straight into a single shared output folder, all of them tagged with the same session timestamp prefix. After a few days of use, the folder fills up with dozens of similarly-named files, and the user has to scan a long flat list to find the one meeting they want. The timestamp prefix tells them *when* the call happened, but nothing about *what* it was.

This change makes two improvements to the same problem:

1. **Each capture session gets its own folder.** All artifacts that belong to a single session (audio sources, combined audio, transcript) live inside that one folder, instead of being scattered into a shared parent folder with shared prefixes.
2. **The folder is given a short, human-readable description** of what the meeting was about, derived from the transcript after the call ends. The folder name starts as the session's timestamp and, once the transcript is in and a short description has been produced from it, the folder is renamed to `<timestamp>-<short-description>` — for example, `2026-05-16-1430-pricing-call-with-acme`.

If any step in the description-generation chain doesn't work (the transcript never arrived, the description couldn't be produced, the folder couldn't be renamed for any reason), the folder simply keeps its timestamp-only name and the session is otherwise unaffected. No alerts, no retries, no user-visible failure.

**Success measure:** After a week of daily use, the user can open their recordings folder and read down the list of subfolders to find a specific call by *subject*, not just by date — and on the rare occasions the description couldn't be produced, the affected folders are still cleanly named by timestamp and still contain all of their files.

---

## 2. Functional Requirements (The "What")

### 2.1 Each capture session lives in its own folder

- **As the user, I want every capture session's files grouped into a single folder of their own, so that one session never mixes visually with another.**
  - **Acceptance Criteria:**
    - [ ] Every successful capture session creates exactly one new folder inside the user's configured recordings location.
    - [ ] All files that belong to that session — the microphone source audio, the system-audio source, the combined audio, and the transcript file(s) when they arrive — are written inside that one folder and nowhere else.
    - [ ] No session writes any file directly into the configured recordings location; all session files live one level deeper, inside their session folder.
    - [ ] Two sessions started on the same calendar day never share a folder; each gets its own.

### 2.2 Initial folder name is timestamp-only

- **As the user, I want a new session's folder to appear as soon as capture stops, with a clear timestamp-based name, so that I can find the recording even if no description gets produced later.**
  - **Acceptance Criteria:**
    - [ ] At the moment `record stop` completes, the session folder exists on disk with a timestamp-only name.
    - [ ] The timestamp used as the folder name is the same timestamp already used today to identify the session.
    - [ ] Sorting the recordings location alphabetically puts session folders in the order they were recorded.

### 2.3 Files inside a session folder use role-only names

- **As the user, I want the files inside a session folder to be named by their role (mic, system, combined, transcript), so that the folder reads cleanly and the timestamp isn't repeated four times inside it.**
  - **Acceptance Criteria:**
    - [ ] The microphone source file inside the folder is named `mic.wav`.
    - [ ] The system-audio source file inside the folder is named `system.wav`.
    - [ ] The combined audio file inside the folder is named `combined.wav`.
    - [ ] The transcript file(s) produced for the session are named by their role/format (e.g., `transcript.txt`, `transcript.json`, `transcript.srt`) and never carry a timestamp prefix.
    - [ ] No file inside a session folder repeats the session's timestamp in its own filename.

### 2.4 Folder is renamed once a short description is ready

- **As the user, I want the session folder's name to gain a short, human-readable suffix describing what the call was about, so that I can recognize a specific meeting at a glance without opening it.**
  - **Acceptance Criteria:**
    - [ ] After the transcript for the session has been written to disk, a short description of the call's subject is produced from the transcript's text content.
    - [ ] On success, the session folder is renamed from `<timestamp>` to `<timestamp>-<short-description>`, and all of the session's files are still inside that same folder under the new name.
    - [ ] The description suffix is lowercase kebab-case (words separated by single hyphens, no spaces, no punctuation, no accented characters).
    - [ ] The description suffix is in English regardless of the language spoken in the meeting.
    - [ ] The description suffix targets 3–6 words and never exceeds 60 characters.
    - [ ] The description suffix summarizes the *subject* of the conversation (what it was about), not metadata like date, time, or participant count.

### 2.5 Silent or near-empty meetings use a stand-in suffix

- **As the user, I want a session whose transcript turned out to be empty or essentially silent to still get a meaningful folder name, so that I can tell at a glance that nothing was said in the call.**
  - **Acceptance Criteria:**
    - [ ] When the transcript exists but contains no recognizable speech (silent recording) or only trivial content too short to summarize, the folder is renamed to `<timestamp>-silent`.
    - [ ] This case is reserved for *intentional silence* in the recording — it is not used to paper over a broken description-generation step.

### 2.6 Any failure leaves the folder at its timestamp-only name

- **As the user, I would rather have a timestamp-only folder than a broken or partially-renamed one, so that a problem in the naming chain never costs me my recording.**
  - **Acceptance Criteria:**
    - [ ] If the transcript file never arrives for the session (for any reason, including the combined audio file having failed per spec 007, transcription having failed, or transcription having been skipped), the session folder keeps its timestamp-only name permanently.
    - [ ] If a short description cannot be produced from the transcript for any reason (e.g., the description-generation step errors out, returns nothing usable, or times out), the session folder keeps its timestamp-only name permanently.
    - [ ] If renaming the folder on disk fails for any reason (e.g., a folder with the target name already exists, the disk is full, or the folder is locked), the session folder keeps its timestamp-only name permanently and no partially-renamed folder is left behind.
    - [ ] In any of the above failure cases, no alert, pop-up, or terminal message about the naming step is shown to the user.
    - [ ] In any of the above failure cases, the reason is recorded in the logs in enough detail to diagnose it later.
    - [ ] In any of the above failure cases, all of the session's files remain inside their folder, intact and accessible.

### 2.7 The stop summary stays focused on the recording itself

- **As the user, I want the terminal summary printed when I stop a capture to look essentially the same as it does today, so that the new naming behavior doesn't add noise to my in-the-moment workflow.**
  - **Acceptance Criteria:**
    - [ ] The terminal summary printed by `record stop` mentions the session folder's path and the per-file detail for the mic, system, and combined files (as today, per spec 007).
    - [ ] The terminal summary contains no line about the upcoming naming step, no line about its eventual success, and no line about its eventual failure.
    - [ ] At the moment `record stop` prints, the folder path shown is the session's current path — i.e., the timestamp-only folder path, since the transcript-driven rename has not happened yet.
    - [ ] The user is expected to understand that the folder may later be renamed; the printed path becoming stale after a later rename is acceptable and is not surfaced as an error.

### 2.8 The rename does not interrupt or delay stopping

- **As the user, I want stopping a capture to feel as immediate as it does today, with the renaming happening quietly later, so that nothing about the naming work blocks me at the end of a call.**
  - **Acceptance Criteria:**
    - [ ] `record stop` returns control to the terminal at the same point it does today (per spec 007, after the combined audio file is finalized) — it does not wait for the transcript to arrive, for a description to be produced, or for the folder to be renamed.
    - [ ] The rename, when it eventually happens, does not require any further action from the user.
    - [ ] If the user starts a new capture before the previous session's rename has happened, both sessions still end up in their own folders with the correct names (the in-flight rename of the older session does not interfere with the new session).

### 2.9 The renamed folder remains stable afterwards

- **As the user, I want a folder's final name to stay put once it has been chosen, so that paths I copy or share don't change under me.**
  - **Acceptance Criteria:**
    - [ ] Once a session folder has been renamed to `<timestamp>-<short-description>` (or to `<timestamp>-silent`), it is never renamed again by the system.
    - [ ] The system does not re-run the description-generation step against an already-named folder (e.g., on the next launch or the next capture).
    - [ ] If the user manually renames a session folder afterwards, the system does not undo or "correct" that.

---

## 3. Scope and Boundaries

### In-Scope

- **A dedicated subfolder per capture session**, created inside the user's configured recordings location.
- **Two-stage folder naming**: a timestamp-only name on capture stop, optionally upgraded to `<timestamp>-<short-description>` once the transcript exists and a description has been produced.
- **Role-only file names** inside each session folder: `mic.wav`, `system.wav`, `combined.wav`, and `transcript.<ext>` (no timestamp prefix on files).
- **Description generation from the transcript text**: short, English, kebab-case, 3–6 words target, 60-character hard cap, describing the subject of the conversation.
- **A `<timestamp>-silent` stand-in** for sessions whose transcripts contain no recognizable content.
- **Quiet, log-only failure handling**: any failure anywhere in the chain leaves the folder timestamp-only, with no terminal output and no user prompt; the cause goes to the logs.
- **Decoupling the rename from `record stop`**: stop returns at the same point it does today; the rename happens later, on its own, when the transcript arrives.

### Out-of-Scope

- **A user-facing setting to disable the AI naming step.** Naming is always attempted; if it fails the folder is timestamp-only, which already covers the "I don't want it" outcome.
- **Surfacing the naming step in the stop summary** (success or failure). The terminal summary is unchanged; the only signal of naming is the folder's eventual name on disk.
- **Retries of a failed description-generation step.** A single attempt is made per session; if it doesn't produce a usable description, the folder stays timestamp-only forever.
- **Re-running description generation against already-named folders**, including historic sessions that pre-date this change. Existing folders are left as they are.
- **Reverting a system-applied folder name** when the user has manually renamed a folder.
- **A separate stand-in suffix for "transcript never arrived"** (e.g. `<timestamp>-no-transcript`). That case is handled the same as any other naming failure: the folder stays timestamp-only.
- **Notifications, alerts, or progress indicators** for the rename. Finder/`ls` is the only surface.
- **Changing the timestamp format or its meaning.** Today's timestamp is reused unchanged as the folder's name and prefix.
- **Changing what gets captured, mixed, or transcribed.** The contents of `mic.wav`, `system.wav`, `combined.wav`, and the transcript are exactly what specs 005, 007, and 004 already define.
- **A meeting-app-aware description** (e.g., pulling Zoom's meeting title or the calendar event name). The description is derived solely from the transcript text.
- **An in-app library, index, search, or browser UI** over the session folders. Phase 4 territory; explicitly out for this work.
- **All other roadmap items**, which are addressed by their own specifications: hotkey-triggered background daemon, primary-display video capture, active meeting window video capture, mixed mic + system audio capture, the cloud-transcription pipeline itself, meeting auto-detection (Zoom, Google Meet, Microsoft Teams) and the user-in-the-loop control around it, generic "any audio call" detection, mic-and-system as separate tracks (already covered by spec 005), voice-based diarization on the system-audio stream, per-speaker transcript tracks, live transcript stream during the call, ML-based source separation, active-speaker cue from video, app-managed library, transcript search, browser/replayer UI, on-device transcription, and on-device speaker diarization.
