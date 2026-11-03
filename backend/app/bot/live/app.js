/* MeetMind bot dashboard: reads /status twice a second and draws the live view. */
(() => {
  const $ = (id) => document.getElementById(id);
  const TINTS = ["#CDEBDD", "#FCE6C2", "#F9D5D1", "#DCE8D2", "#EADFC8", "#D3E6E0"];
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const ICONS = {
    door: "M5 21V4a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v17h3v2H2v-2h3Zm9-9a1 1 0 1 0 0 2 1 1 0 0 0 0-2Z",
    hand: "M13 2a1.5 1.5 0 0 1 1.5 1.5V10h1V4.5a1.5 1.5 0 0 1 3 0V14a8 8 0 0 1-15.2 3.5L1.6 12a1.5 1.5 0 0 1 2.6-1.5L6 13V5.5a1.5 1.5 0 0 1 3 0V10h1V3.5A1.5 1.5 0 0 1 11.5 2H13Z",
    mic: "M12 15a4 4 0 0 0 4-4V5a4 4 0 0 0-8 0v6a4 4 0 0 0 4 4Zm7-4a7 7 0 0 1-6 6.9V21h-2v-3.1A7 7 0 0 1 5 11h2a5 5 0 0 0 10 0h2Z",
    pen: "M3 17.3V21h3.7L17.8 9.9l-3.7-3.7L3 17.3Zm17.7-10.2a1 1 0 0 0 0-1.4l-2.4-2.4a1 1 0 0 0-1.4 0l-1.8 1.8 3.7 3.7 1.9-1.7Z",
    check: "M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4L9 16.2Z",
    alert: "M1 21h22L12 2 1 21Zm12-3h-2v-2h2v2Zm0-4h-2v-4h2v4Z",
  };
  const COPY = {
    starting: ["Getting the bot ready", "Checking the MeetMind server and opening a browser.", "door", "Starting"],
    joining: ["Joining the meeting", "The bot opens your link as a guest called “{bot}”, with its mic and camera off.", "door", "Joining"],
    waiting: ["Waiting to be admitted", "Someone in the call needs to click Admit for “{bot}”. Nothing is recorded until then.", "hand", "Waiting"],
    recording: ["Recording the meeting", "Clean call audio and live captions (only the bot sees them). Leave it running; it stops when the meeting ends.", "mic", "Recording"],
    finishing: ["Saving the last audio", "The bot left the call and is sending the last piece for transcription.", "pen", "Finishing"],
    writing: ["Writing your notes", "Fixing misheard words, naming speakers, then the summary, decisions and tasks.", "pen", "Writing notes"],
    done: ["Your meeting notes are ready", "MOM, Word report, Excel task tracker, README and the full transcript are in one zip.", "check", "Ready"],
    error: ["The bot stopped", "", "alert", "Stopped"],
  };

  // The popup opens this page as /bot/live#token=...&client=... (the token never reaches the server log).
  const params = new URLSearchParams(location.hash.slice(1));
  const HEADERS = { "X-MeetMind-Token": params.get("token") || "", "X-MeetMind-Client": params.get("client") || "live-view-0000" };

  let last = null;
  let lastPhase = "";
  let captionCount = 0;
  let level = 0;

  /* ---------- polling ---------- */
  async function poll() {
    try {
      const response = await fetch("/bot/status", { cache: "no-store", headers: HEADERS });
      if (response.status === 401) {
        $("pill-text").textContent = "Open this page from the MeetMind popup";
        document.body.dataset.phase = "error";
      } else if (response.ok) render(await response.json());
    } catch {
      $("pill-text").textContent = "Bot not running";
      document.body.dataset.phase = "error";
    }
    setTimeout(poll, 500);
  }

  function render(s) {
    last = s;
    const phase = s.phase || "starting";
    document.body.dataset.phase = phase;
    $("code").textContent = s.meeting_code ? `meet/${s.meeting_code}` : "";
    const [head, sub, icon, pill] = COPY[phase] || COPY.starting;
    if (phase !== lastPhase) {
      lastPhase = phase;
      for (const id of ["headline", "subline"]) {
        const el = $(id);
        el.classList.remove("headline-swap");
        void el.offsetWidth;
        el.classList.add("headline-swap");
      }
      document.querySelector(".orb__core span").style.setProperty("--icon", svgUrl(ICONS[icon]));
    }
    $("headline").textContent = phase === "done" && s.report?.title ? s.report.title : head;
    $("subline").textContent = (phase === "error" ? s.error : sub).replace("{bot}", s.bot_name || "MeetMind Notetaker");
    $("pill-text").textContent = pill;
    $("stop-btn").hidden = phase !== "recording";
    $("download-btn").hidden = phase !== "done";
    if (phase === "idle") {
      $("headline").textContent = "The bot is not in a meeting";
      $("subline").textContent = "Send it from the MeetMind popup: paste a Meet link and click Join meeting via bot.";
    }
    renderTimer(s);
    renderTimeline(phase);
    renderPeople(s.people || []);
    renderCaptions(s.captions || []);
    renderAudio(s);
    renderLog(s);
    renderResults(s);
    $("error").hidden = phase !== "error" || !s.error;
    $("error").textContent = s.error || "";
  }

  function renderTimer(s) {
    const start = s.started_at;
    const end = s.ended_at || s.now;
    $("eyebrow-time").textContent = start ? clock(end - start) : s.phase === "waiting" ? "Not recording yet" : "";
  }

  function renderTimeline(phase) {
    const map = { starting: 0, joining: 0, waiting: 1, recording: 2, finishing: 3, writing: 3, done: 4, error: -1 };
    const at = map[phase] ?? 0;
    document.querySelectorAll("#timeline li").forEach((li, i) => {
      li.dataset.state = phase === "done" || i < at ? "done" : i === at ? "active" : "todo";
    });
  }

  function renderPeople(people) {
    const list = $("people");
    if (!people.length) return;
    const max = Math.max(1, ...people.map((p) => p.words || 0));
    const existing = new Map([...list.querySelectorAll(".person")].map((el) => [el.dataset.name, el]));
    list.querySelector(".empty")?.remove();
    people.forEach((p, i) => {
      let row = existing.get(p.name);
      if (!row) {
        row = document.createElement("li");
        row.className = "person";
        row.dataset.name = p.name;
        row.innerHTML = `<span class="avatar"></span><div><div class="person__name"></div><span class="person__bar"><i></i></span></div><span class="person__words"></span>`;
        const avatar = row.querySelector(".avatar");
        avatar.textContent = initials(p.name);
        avatar.style.background = tint(p.name);
        row.querySelector(".person__name").textContent = p.name;
      }
      row.dataset.speaking = String(Boolean(p.speaking) && last.phase === "recording");
      row.dataset.present = String(p.present !== false);
      row.querySelector(".person__bar i").style.width = `${Math.round(((p.words || 0) / max) * 100)}%`;
      row.querySelector(".person__words").textContent = p.words ? `${p.words} words` : "";
      if (list.children[i] !== row) list.insertBefore(row, list.children[i] || null);
    });
  }

  function renderCaptions(lines) {
    const box = $("captions");
    const chip = $("cc-state");
    const final = Boolean(last.captions_final);
    chip.textContent = final ? "Final, corrected" : last.captions_on ? "Captions on · bot only" : "Captions off";
    chip.classList.toggle("chip--on", final || Boolean(last.captions_on));
    $("captions-title").textContent = final ? "Transcript" : "Live transcript";
    if (!lines.length) return;
    box.querySelector(".empty")?.remove();
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    // Update lines already shown (captions grow while someone talks), then add new ones.
    lines.forEach((line, i) => {
      let el = box.children[i];
      if (!el) {
        el = document.createElement("div");
        el.className = "line";
        el.innerHTML = `<span class="avatar"></span><div><b></b><time></time><p></p></div>`;
        box.append(el);
      }
      const avatar = el.querySelector(".avatar");
      if (avatar.textContent !== initials(line.speaker)) {
        avatar.textContent = initials(line.speaker);
        avatar.style.background = tint(line.speaker);
      }
      el.querySelector("b").textContent = line.speaker;
      el.querySelector("time").textContent = clock(line.t);
      el.querySelector("p").textContent = line.text;
    });
    if (lines.length !== captionCount && nearBottom) box.scrollTop = box.scrollHeight;
    captionCount = lines.length;
  }

  function renderAudio(s) {
    const wave = $("wave");
    const levels = s.levels || [];
    if (wave.children.length !== 48) wave.replaceChildren(...Array.from({ length: 48 }, () => document.createElement("i")));
    const padded = [...Array(Math.max(0, 48 - levels.length)).fill(0), ...levels.slice(-48)];
    padded.forEach((v, i) => {
      const bar = wave.children[i];
      bar.style.height = `${Math.max(3, Math.round(v * 60))}px`;
      bar.style.opacity = v > 0.06 ? "1" : "0.35";
    });
    level = levels.length ? levels[levels.length - 1] : level * 0.9;
    $("tracks").textContent = s.tracks ? `Hearing the call (${s.tracks} audio stream${s.tracks > 1 ? "s" : ""})` : "No call audio yet";
    $("engine").textContent = s.engine ? `Speech: ${s.engine}` : "";
    $("pieces").replaceChildren(
      ...(s.pieces || []).map((p) => {
        const el = document.createElement("span");
        el.className = "piece";
        el.dataset.state = p.state;
        el.textContent = `Piece ${p.index + 1} · ${p.note === "silence" ? "silence" : p.state}`;
        el.title = p.error || "";
        return el;
      }),
    );
  }

  function renderLog(s) {
    const events = $("events");
    if (events.children.length !== (s.events || []).length) {
      events.replaceChildren(
        ...[...(s.events || [])].reverse().map((e) => {
          const li = document.createElement("li");
          li.innerHTML = `<time></time><span></span>`;
          li.querySelector("time").textContent = new Date(e.t * 1000).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
          });
          li.querySelector("span").textContent = e.text;
          return li;
        }),
      );
    }
    $("warnings").replaceChildren(
      ...(s.warnings || []).map((w) => {
        const li = document.createElement("li");
        li.textContent = w;
        return li;
      }),
    );
  }

  function renderResults(s) {
    const r = s.report;
    $("results").hidden = !r;
    if (!r) return;
    $("result-title").textContent = r.title || "Meeting notes";
    $("r-length").textContent = minutes(r.duration);
    $("r-people").textContent = r.people;
    const tasks = r.action_items || [];
    $("r-tasks").textContent = tasks.length;
    $("r-fixed").textContent = r.corrections;
    $("r-summary").textContent = r.overview;
    $("r-file").textContent = "The zip has the same files as a recording from the extension.";
    const list = $("r-task-list");
    if (list.children.length !== tasks.length) {
      list.replaceChildren(
        ...tasks.slice(0, 8).map((t, i) => {
          const li = document.createElement("li");
          li.className = "task";
          li.style.animationDelay = `${i * 70}ms`;
          li.innerHTML = `<span class="avatar"></span><div><b></b><small></small></div>`;
          const avatar = li.querySelector(".avatar");
          avatar.textContent = initials(t.owner);
          avatar.style.background = tint(t.owner);
          avatar.style.width = avatar.style.height = "30px";
          li.querySelector("b").textContent = t.task;
          li.querySelector("small").textContent = [t.owner, t.due_date || t.due_text, t.priority, t.verified ? "" : "Please check"]
            .filter(Boolean)
            .join(" · ");
          return li;
        }),
      );
    }
  }

  /* ---------- the listening orb ---------- */
  const canvas = $("orb");
  const ctx = canvas.getContext("2d");
  let smooth = 0;
  function draw(time) {
    const w = canvas.width;
    const c = w / 2;
    ctx.clearRect(0, 0, w, w);
    const phase = last?.phase || "starting";
    smooth += ((phase === "recording" ? level : 0.08) - smooth) * 0.15;
    const t = reduced ? 0 : time / 1000;

    // Soft glow
    const glow = ctx.createRadialGradient(c, c, w * 0.12, c, c, w * 0.5);
    const glowColor = phase === "recording" ? "224,69,58" : phase === "done" ? "233,162,59" : "143,220,188";
    glow.addColorStop(0, `rgba(${glowColor},${0.18 + smooth * 0.3})`);
    glow.addColorStop(1, `rgba(${glowColor},0)`);
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, w);

    if (phase === "recording") {
      // Voice rings that swell with the call's loudness
      for (let i = 0; i < 4; i++) {
        const p = (t * 0.45 + i / 4) % 1;
        ctx.beginPath();
        ctx.arc(c, c, w * (0.2 + p * 0.28 + smooth * 0.05), 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(143,220,188,${(1 - p) * 0.55})`;
        ctx.lineWidth = 2 + smooth * 6;
        ctx.stroke();
      }
      // Radial voice spikes
      const spikes = 64;
      for (let i = 0; i < spikes; i++) {
        const a = (i / spikes) * Math.PI * 2;
        const wobble = Math.sin(a * 5 + t * 6) * 0.5 + Math.sin(a * 3 - t * 4) * 0.5;
        const len = w * (0.03 + smooth * 0.12 * (0.6 + 0.4 * wobble));
        const r0 = w * 0.18;
        ctx.beginPath();
        ctx.moveTo(c + Math.cos(a) * r0, c + Math.sin(a) * r0);
        ctx.lineTo(c + Math.cos(a) * (r0 + len), c + Math.sin(a) * (r0 + len));
        ctx.strokeStyle = i % 8 === 0 ? "rgba(233,162,59,0.9)" : "rgba(143,220,188,0.85)";
        ctx.lineWidth = 3;
        ctx.lineCap = "round";
        ctx.stroke();
      }
    } else if (phase === "writing" || phase === "finishing" || phase === "joining" || phase === "starting") {
      // Orbiting arcs while the bot works
      for (let i = 0; i < 3; i++) {
        const r = w * (0.24 + i * 0.07);
        const start = t * (1.2 - i * 0.3) * (i % 2 ? -1 : 1) + i;
        ctx.beginPath();
        ctx.arc(c, c, r, start, start + Math.PI * (0.5 + i * 0.25));
        ctx.strokeStyle = i === 1 ? "rgba(233,162,59,0.85)" : "rgba(143,220,188,0.75)";
        ctx.lineWidth = 5;
        ctx.lineCap = "round";
        ctx.stroke();
      }
    } else if (phase === "waiting") {
      // A slow dotted ring: knocking, waiting for Admit
      const dots = 28;
      for (let i = 0; i < dots; i++) {
        const a = (i / dots) * Math.PI * 2 + t * 0.4;
        const pulse = (Math.sin(t * 3 - i * 0.45) + 1) / 2;
        ctx.beginPath();
        ctx.arc(c + Math.cos(a) * w * 0.3, c + Math.sin(a) * w * 0.3, 3 + pulse * 4, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(143,220,188,${0.3 + pulse * 0.7})`;
        ctx.fill();
      }
    } else if (phase === "done") {
      // Celebration: a still ring with a few marigold sparks
      ctx.beginPath();
      ctx.arc(c, c, w * 0.27, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(233,162,59,0.9)";
      ctx.lineWidth = 6;
      ctx.stroke();
      for (let i = 0; i < 10; i++) {
        const a = (i / 10) * Math.PI * 2 + t * 0.2;
        const r = w * (0.34 + 0.03 * Math.sin(t * 2 + i));
        ctx.beginPath();
        ctx.arc(c + Math.cos(a) * r, c + Math.sin(a) * r, 4, 0, Math.PI * 2);
        ctx.fillStyle = i % 2 ? "rgba(143,220,188,0.9)" : "rgba(233,162,59,0.95)";
        ctx.fill();
      }
    }
    document.querySelector(".orb__core").style.transform = `scale(${1 + smooth * 0.12})`;
    requestAnimationFrame(draw);
  }

  /* ---------- actions ---------- */
  $("stop-btn").addEventListener("click", async () => {
    $("stop-btn").disabled = true;
    $("stop-btn").lastChild.textContent = "Stopping…";
    await fetch("/bot/stop", { method: "POST", headers: HEADERS }).catch(() => undefined);
  });
  $("download-btn").addEventListener("click", async () => {
    const label = $("download-btn").querySelector("span");
    label.textContent = "Preparing…";
    try {
      const response = await fetch("/bot/files?kind=zip", { headers: HEADERS });
      if (!response.ok) throw new Error(String(response.status));
      const match = /filename\*=UTF-8''([^;]+)/i.exec(response.headers.get("content-disposition") || "");
      const link = document.createElement("a");
      link.href = URL.createObjectURL(await response.blob());
      link.download = match ? decodeURIComponent(match[1]) : "MeetMind_notes.zip";
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 30000);
    } finally {
      label.textContent = "Download all files";
    }
  });

  /* ---------- helpers ---------- */
  function svgUrl(path) {
    return `url("data:image/svg+xml,${encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='${path}'/></svg>`)}")`;
  }
  function initials(name = "") {
    const parts = String(name).split(/\s+/).filter((p) => /^\p{L}/u.test(p));
    return ((parts[0]?.[0] ?? "") + (parts.length > 1 ? parts.at(-1)[0] : "")).toUpperCase() || "?";
  }
  function tint(name = "") {
    let h = 0;
    for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return TINTS[h % TINTS.length];
  }
  function clock(seconds) {
    const s = Math.max(0, Math.floor(seconds || 0));
    return [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60].map((n) => String(n).padStart(2, "0")).join(":");
  }
  function minutes(seconds) {
    const m = Math.round((seconds || 0) / 60);
    return m < 1 ? `${Math.max(1, Math.round(seconds || 0))} s` : m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} m`;
  }

  poll();
  requestAnimationFrame(draw);
})();
