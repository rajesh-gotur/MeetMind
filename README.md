# MeetMind

A free Chrome extension + FastAPI server that turns a Google Meet call into checked meeting notes:
minutes (`MOM.md`), a Word report, an Excel action tracker and a named transcript.

- `backend/` — FastAPI server (also runs the MeetMind bot)
- `extension/` — the Chrome extension, ready to load unpacked
- `extension-source/` — TypeScript source of the extension

**Setup:** see [SETUP_GUIDE.md](SETUP_GUIDE.md).

> API keys are left blank in this repo. Fill them in `backend/config.yaml` and the app token in
> `extension/config.js` on your own machine, and **do not commit them**.
