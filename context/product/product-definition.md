# Product Definition: record

- **Version:** 1.0
- **Status:** Proposed

---

## 1. The Big Picture (The "Why")

### 1.1. Project Vision & Purpose

To give privacy-conscious professionals a local-first, open-source way to record their meetings — capturing video, audio, and a speaker-attributed transcript on their own machine, without sending client conversations through a third-party SaaS like Recall.ai, and **without any visible footprint inside the meeting itself** (no bot joining the call, no banner, no notification to other participants).

### 1.2. Target Audience

Privacy-conscious individuals on macOS — primarily independent consultants and freelancers who run frequent client calls and refuse to upload those conversations to vendor servers. Secondary audience: founders, researchers, and power users who want a self-owned meeting archive that feeds their own notes and AI workflows.

### 1.3. User Personas

- **Persona 1: "Ivan the Independent Consultant"**
  - **Role:** Solo consultant running 4–8 client video calls per day across Zoom, Google Meet, and Microsoft Teams.
  - **Goal:** Walk away from every client call with an accurate, speaker-attributed transcript he can skim to draft follow-ups and capture commitments — without re-listening to the recording.
  - **Frustration:** Existing tools (Otter, Fireflies, Recall.ai) upload client conversations to vendor servers, raising real confidentiality concerns with his clients. They also typically join the call as a visible bot participant, which Ivan finds intrusive and which sometimes causes clients to clam up. Mixed-audio transcription also mangles moments where two people talk over each other — exactly the moments that matter.

### 1.4. Success Metrics

- **Daily personal use is the primary signal.** The product is successful when the user (and pilot users) reach for it on every client call without thinking about it. If it isn't trustworthy or convenient enough for daily use, nothing else matters.
- Recording reliably starts and stops on hotkey without dropped frames or audio gaps.
- The post-meeting transcript is good enough to skim instead of re-listening to the recording.

---

## 2. The Product Experience (The "What")

### 2.1. Core Features

- **Hotkey-triggered background daemon** — a global hotkey starts and stops capture; no menu bar or app window required.
- **Zero meeting-side footprint** — capture is performed entirely on the user's own machine via OS-level screen and audio APIs. The product never joins the meeting as a bot or participant, never injects an extension into the meeting client, and never causes the meeting platform to display a "recording" banner to other attendees.
- **Active-meeting-window video capture** — records only the meeting app's window (not the full screen) to a `.mp4` file.
- **Mixed mic + system audio capture** — captures the user's microphone and the system's audio output as a single mixed audio file (`.wav` / `.m4a`).
- **Cloud-based transcription with speaker diarization** — sends the audio to a cloud transcription API after the call to produce a speaker-attributed transcript with timestamps.
- **Local folder output** — all artifacts (video, audio, transcript) are written to a user-configurable folder on disk. No app database, no upload, no sharing.

### 2.2. User Journey

A consultant is about to start a client call in Zoom/Meet/Teams. Just before joining (or right after), he presses a global hotkey. The `record` daemon, already running in the background, begins capturing the active meeting window's video plus the system audio + microphone. He runs the meeting normally. When the call ends, he presses the hotkey again. The daemon stops capture, writes the video and audio files to his configured local folder, ships the audio to the cloud transcription API, and — when the transcript returns — writes the speaker-attributed transcript alongside the recordings. He opens the transcript to draft his follow-up notes.

---

## 3. Project Boundaries

### 3.1. What's In-Scope for this Version

- **macOS only.**
- **Manual start/stop** via global hotkey (background daemon).
- **Active meeting window video capture** to `.mp4`.
- **Mixed mic + system audio capture** to `.wav` / `.m4a`.
- **Cloud transcription** with speaker diarization, producing a speaker-attributed transcript with timestamps (e.g., `.json` / `.srt`).
- **Local folder storage** at a user-configured path. Files are the artifact.
- **Open source distribution** — public repo, self-installed by the user.

### 3.2. What's Out-of-Scope (Non-Goals)

These are explicitly deferred to keep v1 focused and shippable:

- **Auto-detection of meeting start** (Zoom / Meet / Teams app or "any audio call" detection) — roadmap.
- **Local on-device transcription** (e.g., whisper.cpp) — roadmap; required to fully realize the privacy promise.
- **Live / streaming transcript** during the call — roadmap.
- **Per-participant audio streams** (separate audio per speaker) — roadmap; needed for clean overlapping-speech output.
- **Per-speaker transcript tracks** — roadmap.
- **App-managed library / database / searchable UI** — roadmap.
- **Meeting bots / in-call participants of any kind.** No bot that joins the call, no integration that uses the meeting platform's official recording API (which notifies attendees), no browser extension that surfaces a recording indicator inside the meeting client. Capture is OS-level only.
- **Sharing / collaboration features** (links, shared workspaces, multi-user).
- **Search, summarization, or "chat with meeting" UI** — out; downstream tools are the user's choice.
- **iOS / mobile app.**
- **Windows or Linux support.**
- **Calendar integration.**
- **Webcam picture-in-picture or full-screen capture** (we deliberately scope to the active meeting window).

### 3.3. Known Trade-off (v1)

There is a deliberate tension in v1: the product positions itself as a privacy-first alternative, yet v1 transcription runs through a cloud API. This is a pragmatic build-order choice — getting reliable capture and diarization working end-to-end first, with **on-device transcription as a high-priority roadmap item** to close the privacy gap. Users should be aware that in v1 the audio leaves their machine to be transcribed.
