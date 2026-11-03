/** The bot's screens in the popup: live progress while it works, then the shared results screen. */

import type { BotStatus } from "../shared/types.js";
import { formatClock } from "../shared/transcript.js";
import { $, avatar, element } from "./dom.js";
import type { ResultsModel } from "./results.js";

export const BOT_ACTIVE = new Set(["starting", "joining", "waiting", "recording", "finishing", "writing"]);

const PHASES: Record<string, { label: string; sub: string; step: number }> = {
  starting: { label: "Starting", sub: "Opening the bot's browser…", step: 0 },
  joining: { label: "Joining", sub: "Opening the meeting as a guest…", step: 0 },
  waiting: { label: "Waiting for Admit", sub: "Someone in the call needs to click Admit for {bot}.", step: 1 },
  recording: { label: "Recording", sub: "{bot} is in the call.", step: 2 },
  finishing: { label: "Saving audio", sub: "The meeting ended. Transcribing the last audio…", step: 3 },
  writing: { label: "Writing notes", sub: "Fixing spellings, naming speakers, writing the summary…", step: 3 },
};
const LINES_SHOWN = 3;

/** Live view while the bot works. Returns a ticker to keep the timer moving between polls. */
export function renderBotProgress(bot: BotStatus): () => void {
  const phase = PHASES[bot.phase] ?? PHASES.starting!;
  const name = bot.bot_name || "MeetMind Notetaker";
  $("bot-stage").dataset.phase = bot.phase;
  $("bot-phase").textContent = phase.label;
  $("bot-code").textContent = bot.meeting_code;
  $("bot-sub").textContent = phase.sub.replace("{bot}", name);

  [...$("bot-track").children].forEach((step, i) => {
    (step as HTMLElement).dataset.state = i < phase.step ? "done" : i === phase.step ? "active" : "todo";
  });

  // Until the bot is in the call there is nothing to count yet.
  const inCall = phase.step >= 2;
  $("bot-facts").hidden = !inCall;
  $("bot-lines-block").hidden = !inCall;

  $("bot-people").textContent = String(bot.people.length);
  $("bot-words").textContent = String(bot.people.reduce((sum, p) => sum + (p.words || 0), 0));
  $("bot-cc").textContent = bot.captions_on ? "On" : "Off";
  renderHealth(bot, inCall);

  const lines = bot.captions.slice(-LINES_SHOWN);
  const list = $("bot-lines");
  // Redraw only when the words changed, so the lines do not flicker on every poll.
  const key = JSON.stringify(lines);
  if (lines.length && list.dataset.key !== key) {
    list.dataset.key = key;
    // Only a new speaker turn slides in; a growing sentence just updates its text.
    const shown = new Set([...list.children].map((li) => (li as HTMLElement).dataset.id));
    list.replaceChildren(
      ...lines.map((line) => {
        const id = `${line.speaker}@${line.t}`;
        const item = element("li", "", shown.has(id) ? "lines__item lines__item--still" : "lines__item");
        item.dataset.id = id;
        const body = element("div");
        body.append(element("b", line.speaker), element("p", line.text));
        item.append(avatar(line.speaker), body);
        return item;
      }),
    );
  }

  const warnings = $("bot-warnings");
  warnings.hidden = bot.warnings.length === 0;
  warnings.textContent = bot.warnings.join(" ");

  const stoppable = bot.phase === "waiting" || bot.phase === "recording" || bot.phase === "joining";
  const stop = $<HTMLButtonElement>("bot-stop");
  stop.hidden = !stoppable;
  stop.classList.toggle("btn--stop", bot.phase === "recording");
  stop.classList.toggle("btn--ghost", bot.phase !== "recording");
  $("bot-stop-label").textContent = bot.phase === "recording" ? "Stop and create notes" : "Cancel and remove the bot";

  // The server's clock and this computer's may differ slightly; measure from the server's "now".
  const offset = Date.now() / 1000 - bot.now;
  const timer = $("bot-timer");
  timer.classList.toggle("stage__timer--soft", !bot.started_at);
  const tick = () => {
    const end = bot.ended_at ?? Date.now() / 1000 - offset;
    timer.textContent = bot.started_at ? formatClock(end - bot.started_at) : "Not recording yet";
  };
  tick();
  return tick;
}

/** The watchdog's live check while recording: in the call, sound arriving, captions on. */
function renderHealth(bot: BotStatus, inCall: boolean): void {
  const health = $("bot-health");
  const check = bot.health && "score" in bot.health ? bot.health : null;
  health.hidden = !inCall || !check || bot.phase !== "recording";
  if (!check) return;
  health.dataset.level = check.score >= 85 ? "good" : check.score >= 60 ? "fair" : "low";
  const mark = (ok: boolean, label: string) => `${ok ? "✓" : "✕"} ${label}`;
  health.textContent = `Health ${check.score} · ${mark(check.sound, "Sound")} · ${mark(check.captions, "Captions")}`;
}

export function botResults(bot: BotStatus): ResultsModel {
  const report = bot.report;
  const duration = report?.duration ?? (bot.started_at ? (bot.ended_at ?? bot.now) - bot.started_at : 0);
  return {
    title: report?.title || bot.title || `Meeting ${bot.meeting_code}`,
    durationSeconds: duration,
    people: report?.people ?? bot.people.length,
    overview: report?.overview ?? null,
    tasks: report?.action_items ?? [],
    corrections: report?.corrections ?? 0,
    engine: bot.engine ? `${bot.bot_name}, ${bot.engine}` : "",
    failed: null,
    reportProblem: report ? null : "The notes are not ready.",
    canAnalyzeAgain: false,
    transcriptOnly: false,
    videoNote: null,
    quality: report?.quality ?? null,
    canRenotes: Boolean(bot.meeting_id) && bot.phase === "done",
  };
}
