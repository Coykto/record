# Product Roadmap: record

_This roadmap outlines our strategic direction based on customer needs and business goals. It focuses on the "what" and "why," not the technical "how."_

---

### Phase 1

_The MVP — reliable manual capture that the consultant persona can adopt for daily use. Success criterion: the user reaches for it on every client call._

- [ ] **Capture Foundation**
  - [ ] **Hotkey-triggered background daemon:** A global hotkey starts and stops capture from a daemon already running in the background — no menu bar UI, no app window.
  - [ ] **Primary-display video capture:** Record the user's primary display (the one with the menu bar) to an `.mp4`, paired with the audio file from the same capture session.
  - [ ] **Mixed mic + system audio capture:** Capture the user's microphone and the system's audio output as a single mixed audio file (`.wav` / `.m4a`) suitable for transcription.
  - [ ] **Local folder output with predictable naming:** Write video and audio to a user-configured local folder using a stable naming convention (timestamp-based) so artifacts are easy to find without an in-app library.

- [ ] **Transcript Pipeline V1 (Cloud)**
  - [ ] **Cloud-based transcription with speaker diarization:** After the call ends, send the audio to a cloud transcription provider that returns text segmented by speaker.
  - [ ] **Speaker-attributed transcript file:** Write the diarized transcript with timestamps to the same local folder alongside the audio and video, in a structured format (e.g., `.json` plus a human-readable `.txt` or `.srt`).

---

### Phase 2

_With daily use proven, remove the manual hotkey friction so capture happens automatically when a meeting starts._

- [ ] **Precision Video Capture**
  - [ ] **Active meeting window video capture:** Record only the meeting app's window (Zoom / Meet / Teams) to an `.mp4`, instead of the full primary display. Replaces the Phase 1 primary-display capture once per-window tracking is reliable across the three meeting clients.

- [ ] **Frictionless Capture**
  - [ ] **Meeting auto-detection (Zoom, Google Meet, Microsoft Teams):** Detect when a meeting starts on the three primary platforms and trigger capture automatically — without joining the meeting or appearing inside the meeting client. **This is the must-have for Phase 2.**
  - [ ] **User-in-the-loop control:** Surface a lightweight confirmation (or a "do not record" override) when auto-detection fires, so the user retains explicit control over what is captured.
  - [ ] **Generic "any audio call" detection (investigation required):** Explore detecting any active audio call on the system (e.g., Discord, FaceTime, generic VoIP) by watching for processes that hold the microphone and produce system audio output simultaneously. **Feasibility on macOS is unknown** — this item starts as a research spike, and may be reduced in scope, deferred, or dropped depending on what's discoverable through the OS without per-app integrations. The big three above are not blocked on this.

---

### Phase 3

_Raise transcript fidelity, especially for overlapping speech — the persona's core frustration with mixed-audio transcripts. **Note:** true per-participant audio capture is impossible without joining the meeting (which the product definition forbids), so this phase improves what we can extract from a mixed system-audio stream rather than capturing separate streams per remote participant._

- [ ] **High-Fidelity Transcripts**
  - [ ] **Mic + system audio as separate tracks:** Capture the user's microphone and system audio output as two separate files (not a single mixed stream). Cleanly isolates "you" from "everyone else" — a hard, reliable separation that needs no ML and makes downstream diarization much easier. _(May move into Phase 1 — see open architecture question.)_
  - [ ] **Voice-based diarization on the system-audio stream:** Run a diarization model (e.g., pyannote 3.x with overlap-aware speaker change detection) on the mixed remote-speaker audio to label each segment with a speaker ID. Produces a speaker-attributed transcript even when speakers overlap.
  - [ ] **Per-speaker transcript tracks:** Emit a separate transcript per identified speaker so overlapping segments are preserved verbatim instead of collapsed into one garbled passage.
  - [ ] **Live transcript stream during the call:** Provide a rough live transcript as the meeting is happening, then refine it with diarization after the call ends — useful for in-meeting note-taking.

- [ ] **Quality Research Spikes (exploratory, may not all ship)**
  - [ ] **ML-based source separation for overlap:** Investigate neural source separation (SpeakerBeam, voice-separation models) to recover *approximate* per-speaker audio tracks from the mixed system-audio stream when speakers talk over each other. Quality varies — worth exploring as an upgrade over diarization labels alone.
  - [ ] **Active-speaker cue from video:** Many meeting apps (Zoom, Meet, Teams) highlight the currently-speaking participant's tile with a colored border. Extracting that signal from the video frame (which tile is highlighted at time T) gives a strong cross-modal hint that complements voice-based diarization, especially for short utterances and similar-sounding voices. Per-app, layout-dependent, and CV-heavy — start as a research spike.

- [ ] **Future-work notes (recorded, not committed to a phase)**
  - **Hooking into the meeting client process** for true per-participant audio: intercepting audio at the meeting-client process level (function hooking, audio middleware) could in principle yield true per-participant streams without a visible bot. It is brittle, per-app, breaks on every client update, and likely violates the meeting platforms' Terms of Service. Recorded as a possible future avenue if voice-based diarization + source separation prove insufficient.

---

### Phase 4

_Turn the growing pile of files into a navigable archive the user can actually live in._

- [ ] **Local Archive & Navigation**
  - [ ] **App-managed library:** Move from a flat folder to an app-managed library (metadata + index) that tracks recordings, durations, participants, and transcript locations.
  - [ ] **Transcript search:** Search across all past meeting transcripts locally — find a quote, a name, or a topic across months of calls.
  - [ ] **Simple browser & replayer UI:** A minimal local UI to browse the library, jump to a timestamp in a transcript, and play back the matching audio/video segment.

---

### Phase 5

_Close the privacy gap. The product positions itself as local-first; this phase makes that fully true by removing the cloud dependency from the transcript pipeline._

- [ ] **Truly Local Pipeline**
  - [ ] **On-device transcription:** Run transcription locally (e.g., a Whisper-class model on the user's Mac) so audio never leaves the machine.
  - [ ] **On-device speaker diarization:** Perform speaker separation locally on the same audio so the entire pipeline — capture, transcription, diarization — runs without any network call.
