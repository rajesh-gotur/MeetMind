# MeetMind 5.0 — Accuracy & Reliability Enhancement Plan

> **Status: implemented in v5.0.0.** The code follows this plan; see `backend/app/pipeline/` (one file per
> layer) and `SETUP_GUIDE.md`. Section 7 shows the real `config.yaml` keys.

> **Read this first (for any AI assistant or developer).**
> This file explains **what** we are building in MeetMind 5.0, **why**, and **how**. It is the single source of truth for the update.
> Owner: Suku (Frappe/ERPNext developer). Keep replies in **simple English**, stay inside this plan (no extra features), and build it as **one update** (not in phases).

---

## 1. What MeetMind is (today, v4.1)

MeetMind turns a Google Meet call into finished meeting notes:

| Output | Content |
|---|---|
| `MOM.md` | Minutes: attendees, summary, discussion by topic, decisions, action items, next steps |
| `README.md` | Short overview + task checklist |
| `Meeting_Report.docx` | Word report (screenshots in video mode) |
| `Action_Tracker.xlsx` | Tasks with owner, due date, priority, status |
| `Transcript.txt` | Every line with speaker name and time |

**Two ways to record (chosen in the Chrome extension popup):**

1. **MeetMind bot** — the user pastes any Meet link, clicks *Join meeting via bot*. The server opens the user's real Google Chrome, joins **as a guest** ("MeetMind Notetaker"), a host clicks *Admit*, and the bot records.
2. **Record here** — the extension records the Meet tab + the user's microphone (two separate tracks) in the user's own browser.

**Stack**

- **Extension:** Chrome MV3, TypeScript (built with `tsc`), popup + service worker + offscreen recorder + content script.
- **Backend:** Python FastAPI (`backend/`), started with `python run.py` on the user's Windows laptop (VS Code terminal). Config in `backend/config.yaml`.
- **Bot:** Playwright/patchright **connects** (CDP) to real Chrome started as a normal program (no automation flags). Bot page code runs in a **hidden isolated world** (Meet's scripts can't see it). Audio = **tab audio** via `getDisplayMedia(preferCurrentTab)` with Chrome switch `--auto-accept-this-tab-capture`.
- **AI today:** Deepgram Nova-3 → Groq Whisper (speech-to-text), Gemini → Groq (notes), one notes call per meeting.

**Hard constraints**

- **Free** (free tiers / free credit / open source). No paid plan required.
- **Guest only** — the bot never signs in. If a meeting does not allow guests, it says so clearly and stops.
- Runs on a normal **Windows laptop**, probably **no GPU**.
- **Never ship API keys or app tokens** in shared files. Keys live only in `backend/config.yaml` (or env vars).
- Meet captions are turned on **only inside the bot's own window** (nobody else sees them). In *Record here* mode captions stay **off** (owner's choice).

---

## 2. Why we are doing this (problems found in real meetings)

| # | What happened | Root cause | Lesson |
|---|---|---|---|
| 1 | Words wrong: "ERPNex", "PRAPE", wrong speakers | One speech engine, weak vocabulary use, names guessed | Need several independent "ears" + glossary + name signals |
| 2 | Bot refused: "You can't join this video call" | Automation flags, inhuman typing speed, reused browser profile | Join like a real person in a fresh guest browser |
| 3 | Bot thought it was *in the call* while still in the waiting room | New waiting room also shows a hang-up button | "Admitted" only when **2 signals** agree |
| 4 | Notes empty / "No speech was recorded" | Meet now plays audio via **Web Audio** (no `<audio>` elements) → bot captured silence | Record tab audio; add health checks |
| 5 | Popup showed the whole conversation live (captions) but notes had **only the greeting** | Notes used **only** the recorded audio; when audio failed, the perfect captions were thrown away | **Never depend on one signal.** Captions must back up audio |
| 6 | Failures were silent | No logs / no health checks / results only in memory | Watchdog, logs, saved files, quality scores |

**The one rule for 5.0:** *collect several independent signals → compare → score → verify → only then write the notes.*

---

## 3. Research that shaped this plan (key facts)

- **Gemini 3.5 Transcribe** (Google, model `gemini-3.5-transcribe`): custom vocabulary up to 1,000 terms (best ≤100), 85+ languages with code-switching, accepts WebM, word timestamps, diarization (up to 8, 3+ experimental). Google reports ~5% WER (FLEURS) vs 15.8% for Deepgram Nova-3 (streaming chart). Custom vocabulary **cannot** be combined with diarization / word timestamps in one request. Free tier listed (quota unknown); paid ≈ $0.003/min.
- **Hindi benchmark (16 Indic models):** AI4Bharat **IndicConformer** 14.4% WER (best, open weights), Sarvam 14.7%, **Deepgram Nova-3 25.6% (last of 9)**. Whisper showed script problems on Indian languages.
- **Meet captions** are lossy (Google paraphrases/truncates); in Recall.ai's test captions ≈ 82% vs audio ASR ≈ 83% → use as **second opinion + backup**, not as final text.
- **Generative error correction** (HyPoradise NeurIPS 2023, Cambridge 2024): an LLM fixes ASR errors best when it sees **several hypotheses**.
- **FRAME (EMNLP 2025):** facts → score → outline → write → reviewer, reduced hallucination/omission in meeting summaries.
- **Vexa (open-source Meet bot):** leaves speaker **empty** under heavy crosstalk instead of guessing (~4–7% of rows).
- **pyannote Community-1:** ~17% DER on AMI but needs GPU → not used (Meet gives real names for free).

---

## 4. The 5.0 pipeline — 12 layers in 6 stages

After **Stop**, the saved recording + signals pass through every layer. Each layer fixes what the previous one missed. **Every AI step has a free backup chain.** Heavy local models run **only on doubtful parts**.

### Stage 0 — During the meeting (capture + health)

| Signal | Source | Notes |
|---|---|---|
| Call sound | Tab audio, **2-minute pieces with ~2 s overlap** | Record here: tab + mic tracks |
| Captions with names | Bot's own window | Exact first/last-seen times per caption |
| Who-is-talking timeline | Meet tile "speaking" highlight | name + start/end |
| People list | Meet participants | join/leave times |

- **Watchdog every 5 s:** still in call? sound level alive? captions on? → self-repair (restart tab audio, re-enable captions) + **live health score** in popup.
- **Everything saved to disk** per meeting in `backend/meetings/<id>/` (audio pieces, captions, timeline, transcripts, notes). Auto-deleted after **7 days**. Enables **"Create notes again"**.

### Stage 1 — Clean the audio

| Layer | Tool (free) | Job |
|---|---|---|
| **L1 Prepare** | **PyAV** (bundled FFmpeg) | Decode WebM → mono 16 kHz, loudness-normalize |
| **L2 Find speech** | **Silero VAD** (ONNX, CPU) | Speech regions, cut at natural pauses (no split words), drop silence (stops engines inventing text) |

### Stage 2 — Hear it several times (independent ears)

| Layer | Tool | Runs on | Gives |
|---|---|---|---|
| **L3 Ear 1** | **Gemini 3.5 Transcribe** + custom vocabulary (glossary + attendee names + meeting title + learned terms) | All speech | Best words, Hinglish |
| **L4 Ear 2** | **Deepgram Nova-3** + keyterms, `language=multi` | All speech | Word timings, word confidence, voice groups |
| **L5 Ear 3** | **Meet captions** (bot) | Already captured | Second opinion + real names |
| **L6 Specialist ears** | **AI4Bharat IndicConformer** (Hindi-heavy spans) and **faster-whisper** (English spans) | **Only low-agreement spans** | Extra votes where it matters |

Backup chains: Gemini Transcribe → Groq Whisper → local faster-whisper. Deepgram fails → use Gemini/whisper timings.

### Stage 3 — Merge and correct

| Layer | Tool | Job |
|---|---|---|
| **L7 Word vote (ROVER-style)** | Alignment + scoring | Align all hypotheses word-by-word; score = ear agreement + engine confidence + glossary match; best word wins; each word keeps a confidence |
| **L8 Glossary fixer** | **RapidFuzz + Jellyfish** (spelling + phonetic) | Fix against glossary + attendee names + **learned glossary** (confirmed corrections from past meetings) |
| **L9 AI fixer** | **Gemini** → Groq Llama → local **Ollama** | Sees all versions of **only low-score spans**, chooses/fixes words; existing safety checker rejects meaning changes |

### Stage 4 — Who said what

| Layer | Job |
|---|---|
| **L10 Speaker vote** | Per word: caption name + who-is-talking timeline + voice group. Map voice groups → people by overlap ("Speaker 1 = Rajesh 92%"). Record here: mic track is always the user. Crosstalk with no agreement → **"(unclear)"**, never a wrong name |

### Stage 5 — Notes (facts first, then verified)

| Layer | Tool | Job |
|---|---|---|
| **L11 Facts → notes (FRAME)** | **Gemini** → Groq → Ollama | Topic split → extract facts with quote + time → label (Decision / Task / Insight / Context) + importance 1–10 → outline → write MOM, tasks (owner, due), next steps |
| **L12 Second-opinion check** | A **different** model (Groq Llama or Ollama) + **RapidFuzz** + **dateparser** | Every quote must exist in transcript; owners must be attendees; dates → real calendar dates ("next Friday" → date); every topic covered; unsupported items removed, missing added |

### Stage 6 — Scores and files

- **Quality scores** (popup + "Quality" section in MOM and Word):
  - **Capture:** % of meeting recorded
  - **Words:** % of words confirmed by ≥2 ears
  - **Speakers:** % of lines with name confirmed by ≥2 signals
  - **Notes:** % of tasks/decisions backed by a verified quote
  - **Overall: 0–100** (estimated) + list of any layer that was skipped or fell back
- Same files as today. Transcript marks doubtful words, e.g. `[ERPNext?]`.

### Stop → notes rules

1. Wait **5 s** after Stop for last captions (they lag ~2 s).
2. Save last audio piece; wait for **all** transcriptions (with retries).
3. Run L1–L12. Show steps in popup.
4. **Never empty:** if audio failed, build notes from captions + timeline, with a warning.

---

## 5. Bot reliability rules

- **Guest only.** No sign-in, no `bot-login`. Not allowed → clear message, stop.
- Pre-flight check (link, Chrome, keys, server) before joining.
- Join like a person: fresh guest profile each try, natural mouse + key-by-key typing, ~3 s "reading" pause, mic/camera left on "ask" (never allowed), pop-ups answered "without microphone and camera".
- Refused → retry up to 3× in a **new** fresh browser (no page reload).
- **Admitted only when 2 signals agree** (call toolbar + people list visible, twice).
- Every step, warning and failure is printed in the server window.

---

## 6. Expected accuracy (honest targets)

These are **targets/estimates** based on the research above and the layer design. **Real numbers are measured** with the test set (Section 8) and shown in the Quality scores.

| Measure | v4.1 (approx.) | 5.0 target |
|---|---|---|
| Empty or missing notes when the bot was in the call | Happened | **0** (captions backup) |
| Word accuracy — English / Indian-English meetings | ~85–90% | **~93–96%** |
| Word accuracy — Hinglish (mixed) meetings | ~75–85% | **~87–92%** |
| Glossary / project terms spelled right (ERPNext, Frappe, …) | Often wrong | **≥98%** |
| Lines with the right speaker (bot, captions on) | ~85% | **≥95%** (rest "(unclear)", not wrong) |
| Lines with the right speaker (no captions) | ~70% | **≥85%** |
| Tasks captured (recall) | Unmeasured | **≥90%** |
| Tasks correct — real, right owner (precision) | Unmeasured | **≥95%** |
| Bot join success when guests are allowed | Low | **≥90%** (Google's own check can still refuse some tries) |

Limits: heavy crosstalk, very poor audio, and Google refusing a guest are never fully solvable; the system **flags** these instead of hiding them.

---

## 7. Configuration (backend/config.yaml) — new keys in 5.0

```yaml
gemini:
  transcribe_model: gemini-3.5-transcribe   # Ear 1 ("" = skip)
  transcribe_languages: [en-IN, hi-IN]      # used when the extension says "Auto"
pipeline:
  ears: {gemini: true, deepgram: true, captions: true, groq_whisper: true, local_whisper: auto, indic: auto}
  voice_detection: true      # Silero VAD
  glossary_fixer: true       # RapidFuzz + Jellyfish
  ai_fixer: true             # L9
  doubt_below: 0.72          # words below this score are double-checked
vocabulary:
  learn: true                # learned_terms.json
notes:
  review: true               # reviewer AI (a different model: Groq first)
  section_minutes: 12
storage: {enabled: true, folder: meetings, keep_days: 7}
quality: {show_in_files: true}
ollama: {enabled: auto, model: llama3.1:8b, base_url: http://localhost:11434}
local_models: {whisper_model: small, whisper_compute: int8, indic_model: ai4bharat/indic-conformer-600m-multilingual}
bot:
  piece_seconds: 120         # 2-minute pieces with a 2 s overlap
  # join_as / bot-login were removed: the bot always joins as a guest
```

Install: `requirements.txt` (light tools: av, onnxruntime, rapidfuzz, jellyfish, dateparser, patchright/playwright)
and optionally `requirements-extra.txt` (faster-whisper, transformers, torch, torchaudio). The Silero VAD model
file (MIT) is bundled in `backend/app/pipeline/models/`.

## 8. How it is tested (before release)

- Full bot meeting on the test Meet page (Web-Audio playback like real Meet, waiting room with hang-up button), name checks.
- **Sound cut halfway** → notes still complete from captions.
- **Hinglish + glossary** lines ("ERPNex" → "ERPNext").
- **Crosstalk** → "(unclear)", not a wrong name.
- **Each AI engine switched off in turn** → backup chain takes over.
- Refused guest join → clean retries → clear message.
- Stop while the meeting is still running.
- Record here mode; server unit tests; extension unit tests; no secrets / no test files in the zip.
- **Developer test set:** real meetings (with consent) + hand-corrected answer keys, scored with `jiwer` (WER), glossary %, speaker %, task precision/recall. A change ships only if the numbers improve.

---

## 9. Code map (where changes go)

| Area | Files |
|---|---|
| New pipeline | `backend/app/pipeline/` — `audio.py` (L1), `vad.py` (L2), `ears.py` (L3, L4, L6), `captions.py` (L5), `fusion.py` (L7), `glossary.py` (L8), `ai_fix.py` (L9), `speakers.py` (L10), `facts.py` (L11), `verify.py` (L12), `transcriber.py` (runs L1–L8), `scores.py`, `store.py`, `memory.py` (Record-here doubts) |
| Bot | `backend/app/bot/session.py` (join, record, watchdog, save), `finalize.py` (L5–L12 + "Create notes again"), `browser.py`, `page_hooks.js` (tab audio, overlap, timeline, captions), `links.py` |
| Notes / exports | `backend/app/services/notes.py`, `llm.py`, `exporters/` (Quality section) |
| API | `backend/app/api/` — health score, "create notes again" |
| Extension | `extension-source/src/popup/*` (health + quality scores, "Create notes again"), `shared/types.ts` + `backend/app/schemas.py` (**change together**) |
| Config | `backend/app/config.py`, `backend/config.yaml`, `requirements*.txt` |
| Docs | `SETUP_GUIDE.md`, `START_HERE.txt`, `KNOWLEDGE.md` |

---

## 10. Working rules for whoever implements this

1. Build the **whole plan in one update**; then test everything (Section 8); then deliver **one clean zip** (no tests, no dummy files, no keys except the owner's own config).
2. Keep the existing behaviour and file formats; only add what is in this plan.
3. Every AI call: timeout, retry, backup chain, and a log line.
4. Never let a failure be silent: popup message + server log + Quality section.
5. Explain things to the owner in **short, simple English**.
