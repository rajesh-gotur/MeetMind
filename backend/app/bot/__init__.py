"""The MeetMind bot: joins a Google Meet call as a guest, records it, and writes the notes.

Modules:
  manager.py   one bot at a time, on its own thread and event loop (started from the extension)
  session.py   the steps: join -> record -> transcribe -> notes
  browser.py   everything that touches the Meet page (Playwright)
  page_hooks.js runs inside the Meet page: call audio, captions, people, screen state
  speakers.py  gives every word a speaker (captions first, then voices)
  state.py     live status shown in the popup and the live view
"""
