# MeetMind 5.0 — Setup and User Guide

MeetMind turns a Google Meet call into finished, checked meeting notes:

| File | What it is |
| --- | --- |
| `MOM.md` | Minutes: attendees, summary, discussion by topic, decisions, action items, next steps, **Quality** |
| `README.md` | Short overview with a task checklist |
| `Meeting_Report.docx` | Word report (screenshots in video mode) with a **Quality** table |
| `Action_Tracker.xlsx` | Tasks with owner, real due date, priority and status |
| `Transcript.txt` | Every line with the speaker's real name and the time |

**What is new in 5.0:** every recording passes through **12 accuracy layers** (Section 9). Several speech engines
listen to the same audio and vote word by word, a glossary fixer and an AI fixer correct doubtful words, a
speaker vote names every line, and the notes are written from verified facts and checked by a second AI.
Every meeting gets a **Quality score** (0–100) and is saved for 7 days, so **Create notes again** always works.

## Two ways to record

| | **MeetMind bot** (most accurate) | **Record here** |
| --- | --- | --- |
| How | Paste any Meet link, click **Join meeting via bot** | Click **Start recording** on the Meet tab |
| Joins as | A guest, "MeetMind Notetaker" (someone clicks Admit) | You |
| Ears | Gemini + Deepgram + **Meet captions** (only the bot sees them) | Gemini + Deepgram (your mic and the call separately) |
| Names | Captions + who-is-talking + voice groups | Your mic is always you; others from tiles and voices |
| If the sound fails | Captions fill the gap; the watchdog repairs the capture | The piece is kept and retried |
| Video and screenshots | No | Yes, with **Audio and video** |

---

## 1. What is in the zip

```
MeetMind/
├── SETUP_GUIDE.md            This guide
├── START_HERE.txt            The short version
├── ENHANCEMENT_PLAN.md       What 5.0 changed and why (useful context for any AI assistant)
├── render.yaml               Optional: free hosting on Render (without the bot)
├── backend/                  The server (Python, FastAPI) — also runs the bot
│   ├── config.yaml           <- the ONLY server file you edit (Quick Setup block at the top)
│   ├── run.py                Start or check the server
│   ├── requirements.txt      Needed packages
│   ├── requirements-extra.txt  Optional heavy local models
│   └── app/
│       ├── pipeline/         The 12 accuracy layers (one small file each)
│       ├── bot/              The meeting bot
│       ├── services/         AI calls, notes, checks
│       └── exporters/        Word, Excel, MOM, README, transcript
├── extension/                The Chrome extension, ready to load
│   └── config.js             <- the ONLY extension file you edit
└── extension-source/         TypeScript source of the extension
```

## 2. What you need

- Windows, macOS or Linux with Google Chrome 116 or newer
- Python 3.11 or newer
- Free accounts: Google (Gemini), Deepgram, Groq

## 3. Get the free API keys

| Key | Where | Used for |
| --- | --- | --- |
| Gemini | https://aistudio.google.com/apikey | Ear 1 (Gemini 3.5 Transcribe) and writing the notes |
| Deepgram | https://console.deepgram.com → API Keys | Ear 2 (exact word timings); $200 free credit |
| Groq | https://console.groq.com/keys | Backup ear and the **reviewer AI** (a second model) |

All three are recommended. With fewer keys MeetMind still works and the Quality section says which layer was skipped.

## 4. Configure

Open `backend/config.yaml`. Everything you must fill in is in the **QUICK SETUP** block at the top:

```yaml
server:
  app_token: "a-long-random-text"    # python -c "import secrets; print(secrets.token_urlsafe(32))"
gemini:
  api_key: "..."
deepgram:
  api_key: "..."
groq:
  api_key: "gsk_..."
```

Then open `extension/config.js` and put the **same** app token in `appToken`.

Add your own words to `vocabulary.terms` (client names, colleagues, project words). MeetMind also **learns**
terms it corrected (saved in `backend/learned_terms.json`).

## 5. Install and start the server

Use a folder **outside OneDrive**, for example `C:\MeetMind`. In PowerShell or the VS Code terminal:

```powershell
cd C:\MeetMind\MeetMind\backend
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium
python run.py
```

If PowerShell says "running scripts is disabled": `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again.
Use `python -m pip` (not plain `pip`): it always works, even when `pip` is not found.

The banner shows every layer:

```
MeetMind backend v5.0.0
Folder          : C:\MeetMind\MeetMind\backend
Keys            : Gemini AQ.A...wxyz, Deepgram 1234...abcd, Groq gsk_...wxyz
Ears            : Gemini gemini-3.5-transcribe + Deepgram nova-3 + Meet captions (bot)
Backup ears     : Groq whisper-large-v3
Layers          : audio prep PyAV, voice detection Silero VAD, glossary fixer on, AI fixer on
Notes           : facts first, reviewer AI on
Saved meetings  : ...\backend\meetings (kept 7 days)
MeetMind bot    : ready, joins as a guest "MeetMind Notetaker"
App token       : abcd...wxyz (from config.yaml; must match appToken in extension/config.js)
```

Keep this window open. Every step of every meeting is printed here. If port 8000 is already used (an old
server), `run.py` stops and tells you how to close it. `python run.py check` checks the config without starting.

### Optional: local models (offline backup, extra Hindi ear)

```powershell
python -m pip install -r requirements-extra.txt
```

- **faster-whisper** listens again to doubtful English parts and is the offline backup.
- **AI4Bharat IndicConformer** listens again to doubtful Hindi parts (best open Hindi model in public benchmarks).
  It is gated on Hugging Face: accept its terms once at the model page and run `huggingface-cli login`.
- **Ollama** (https://ollama.com, `ollama pull llama3.1:8b`) is the last backup for the AI steps.

MeetMind uses them automatically when installed (`auto` in `config.yaml`); they never run on the whole meeting,
only on doubtful parts, so a normal laptop can handle them. The first use downloads the models.

## 6. Install the extension

1. `chrome://extensions` → **Developer mode** on → remove any old MeetMind → **Load unpacked** → the `extension` folder.
2. Details must say **Version 5.0.0**.
3. The setup page must show all green ticks (server, token, speech-to-text, notes).

## 7. Record with the MeetMind bot

1. Click the MeetMind icon and choose **MeetMind bot**.
2. Paste any Meet link — `meet.google.com/abc-defg-hij`, `meet.google.com/meet/abc-defg-hij`, links with
   `?authuser=…`, `g.co/meet/…` or just the code. The box shows **Meeting abc-defg-hij found**.
3. **Join meeting via bot**. A Chrome window opens on the server's computer and asks to join as a guest
   (it waits like a person, types its name, clicks Ask to join). Leave the window open; you can minimise it.
   A small "Sharing this tab" bar in the bot's window is the bot recording the call sound.
4. Click **Admit** in the meeting. The popup shows **Recording** with a live line such as
   `Health 100 · ✓ Sound · ✓ Captions`.
5. It stops when the meeting ends or everyone else leaves; **Stop and create notes** stops earlier
   (the bot waits 5 s for the last captions).
6. Download the files. **Create notes again** rewrites the notes from the saved meeting at any time
   (the speech engines are not called again).

If Meet refuses the bot ("You can't join this video call"), the bot retries 3 times in a fresh browser. If it
is always refused, open the link in a Chrome **Guest** window: if that is refused too, the meeting does not
allow guests (Host controls → Meeting access).

## 8. Record in this browser

On the Meet tab: MeetMind → **Record here** → **Audio** or **Audio and video** → **Start recording**.
Click **Stop and create notes** at the end.

## 9. The 12 accuracy layers

| # | Layer | Tool (free) | What it does |
| --- | --- | --- | --- |
| L1 | Clean audio | PyAV | Mono, 16 kHz, steady loudness |
| L2 | Find speech | Silero VAD | Finds speech, skips silent pieces (no engine call, nothing invented) |
| L3 | Ear 1 | Gemini 3.5 Transcribe | Best words and Hinglish; your glossary as custom vocabulary |
| L4 | Ear 2 | Deepgram Nova-3 | Exact word times, confidence per word, voice groups |
| L5 | Ear 3 | Meet captions (bot) | Second opinion with real names; fills gaps when the sound fails |
| L6 | Extra ears | faster-whisper, IndicConformer (optional) | Listen again only to doubtful parts |
| L7 | Word vote | — | Lines up every ear word by word; agreement + confidence + glossary decide |
| L8 | Glossary fixer | RapidFuzz + Jellyfish | "ERP next" → ERPNext; sound-alike fixes on doubtful words only |
| L9 | AI fixer | Gemini → Groq → Ollama | Chooses between the ears' versions of doubtful words; meaning changes are rejected |
| L10 | Speaker vote | — | Captions + who-is-talking + voice groups; crosstalk is marked "Unclear", never a wrong name |
| L11 | Facts first | Gemini | Facts with quotes and times, each checked against the transcript; the notes are written from them |
| L12 | Second opinion | Groq (a different AI) + RapidFuzz + dateparser | Missing tasks added, unsupported ones removed, "next Friday"/"kal" → real dates |

Every AI step has a backup chain, so one service being down never stops a meeting.

## 10. Quality scores

Shown in the popup and in MOM.md and the Word report:

| Score | Meaning |
| --- | --- |
| Recorded | Share of the meeting with usable sound or captions |
| Words | Share of words confirmed by 2+ ears (or very high confidence) |
| Speakers | Share of words named by Meet's captions or 2+ agreeing signals |
| Notes | Share of tasks backed by a verified quote |
| Overall | Weighted total (estimated) |

Below the scores MeetMind lists anything that went wrong or was skipped, for example
"The call sound stopped for a while; the bot repaired it and captions filled the gap."

## 11. Settings (backend/config.yaml)

| Setting | Default | What it does |
| --- | --- | --- |
| `pipeline.ears.*` | all on | Switch single ears on or off (`local_whisper` / `indic`: `auto`) |
| `pipeline.doubt_below` | `0.72` | Words scored below this are double-checked |
| `pipeline.ai_fixer` | `true` | AI chooses between heard versions of doubtful words |
| `vocabulary.terms` | ERPNext, Frappe… | Words that must be spelled exactly |
| `vocabulary.learn` | `true` | Remember corrected terms for next meetings |
| `notes.review` | `true` | A second, different AI checks the notes |
| `notes.section_minutes` | `12` | Long meetings are read in sections |
| `storage.keep_days` | `7` | Saved meetings are deleted after this |
| `quality.show_in_files` | `true` | Quality section in MOM.md and Word |
| `ollama.enabled` | `auto` | Local AI backup when it is running |
| `bot.piece_seconds` | `120` | Audio is heard in 2-minute pieces during the call |
| `bot.headless` | `false` | Keep false; Meet refuses hidden browsers more often |
| `bot.profile_dir` | `bot-profile` | Keep outside OneDrive, e.g. `C:/MeetMind-bot-profile` |

## 12. Troubleshooting

| Problem | Fix |
| --- | --- |
| Banner shows an older version, "App token does not match", or `/bot/status 404` | An old server is answering. Close all terminals, `netstat -ano \| findstr :8000`, `taskkill /F /PID <number>`, start `python run.py` from this folder |
| `pip` is not recognized | Use `python -m pip install -r requirements.txt` |
| Bot: "Google Meet refused the bot's guest browser" | Test the link in a Chrome Guest window; if refused too, the meeting does not allow guests |
| Bot: "No call audio has reached the bot yet" | Check the bot's window is in the call and shows "Sharing this tab" |
| Lines show **Unclear** | People talked over each other; MeetMind marks it instead of guessing |
| A word is still misspelled | Add it to `vocabulary.terms`; it is then used by every ear and fixer |
| Quality lists a skipped layer | Usually a missing or out-of-quota key; the server window shows the exact error |
| Old popup after an update | `chrome://extensions` → Remove MeetMind → Load unpacked the new `extension` folder |

## 13. Hosting on Render (optional)

`render.yaml` deploys the server without the bot (`BOT_ENABLED=false`). Set the keys and `APP_TOKEN` in the
Render dashboard and use **Record here**.

## 14. How it was tested

- **Layer tests (56):** audio prep, voice detection, word vote (misheard words, missed stretches, sound loss),
  glossary and learned terms, AI fixer (and its safety checks), speaker vote (crosstalk → Unclear), facts and
  quote checks, coverage, dates ("by next Monday", "kal", "end of month", "15th"), scores, saving, every engine
  failing in turn, the reviewer using a different model, Ollama as the last backup.
- **Bot, end to end (25 checks):** the real extension sent the real bot into a test Meet page with WebRTC voices
  played through Web Audio (like Meet today), a waiting room with a hang-up button, lagged captions and a
  speaking highlight. Deepgram's "ERPNex" was outvoted to "ERPNext", every line had the right name, a task the
  writer AI missed was added from the verified facts, all files downloaded, Create notes again worked without
  calling the speech engines.
- **Hard scenarios (10 checks):** sound lost halfway (later lines and tasks recovered from captions, watchdog
  repaired, Quality explained it), Stop during the meeting, refused once then admitted, always refused.
- **Record here, end to end (18 checks):** both tracks heard by both ears with the glossary, misheard word fixed,
  facts + writer + reviewer, quality score.
- Not tested: live Google Meet and the real Gemini/Deepgram/Groq services. Check the Quality section of your
  first meetings.

## 15. Privacy

- Tell everyone before you record. The bot's name says it is a notetaker.
- Captions the bot turns on are visible only in its own window.
- Meetings are saved on your computer (`backend/meetings`) for `storage.keep_days` days, then deleted.
  Set `storage.enabled: false` to keep nothing (Create notes again then does not work).
- Audio is sent to the AI services you configured. Free tiers may use data to improve their products; avoid
  confidential meetings on free tiers.
