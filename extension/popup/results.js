/**
 * The "notes are ready" screen. The same screen (and the same files) serves both a
 * recording made in this browser and a meeting recorded by the MeetMind bot.
 */
import { $, avatar, element, formatDue, formatDuration } from "./dom.js";
const TASKS_SHOWN = 4;
const QUALITY_PARTS = [
    ["capture", "Recorded"],
    ["words", "Words"],
    ["speakers", "Speakers"],
    ["notes", "Notes"],
];
export function renderResults(model) {
    const hasReport = model.overview !== null;
    $("done-title").textContent = model.title;
    $("stat-duration").textContent = formatDuration(model.durationSeconds);
    $("stat-speakers").textContent = String(model.people);
    $("stat-tasks").textContent = hasReport ? String(model.tasks.length) : "–";
    $("stat-fixed").textContent = hasReport ? String(model.corrections) : "–";
    const engine = $("engine-note");
    engine.hidden = !model.engine;
    engine.textContent = model.engine ? `Recorded with ${model.engine}` : "";
    $("failed-card").hidden = !model.failed;
    $("failed-text").textContent = model.failed?.message ?? "";
    $("report-error").hidden = !model.reportProblem;
    $("report-error-text").textContent = model.reportProblem ?? "";
    $("analyze-btn").hidden = !model.canAnalyzeAgain;
    $("summary-card").hidden = !hasReport;
    $("summary-text").textContent = model.overview ?? "";
    renderTasks(model.tasks, hasReport);
    for (const button of document.querySelectorAll(".file[data-kind]")) {
        button.disabled = button.dataset.kind === "transcript" ? !(hasReport || model.transcriptOnly) : !hasReport;
    }
    $("download-all").disabled = !hasReport;
    $("download-all-label").textContent = hasReport ? "Download all files" : "Notes are not ready";
    const video = $("video-note");
    video.hidden = !model.videoNote;
    video.textContent = model.videoNote ?? "";
    renderQuality(hasReport ? model.quality : null);
    $("renotes-btn").hidden = !model.canRenotes;
}
/** Scores 0-100 from comparing independent signals (estimated, not a human check). */
function renderQuality(quality) {
    const card = $("quality-card");
    card.hidden = !quality;
    if (!quality)
        return;
    card.dataset.level = level(quality.overall);
    $("quality-overall").textContent = String(quality.overall);
    $("quality-bars").replaceChildren(...QUALITY_PARTS.map(([key, label]) => {
        const value = Number(quality[key]) || 0;
        const row = element("li", "", "quality__row");
        row.dataset.level = level(value);
        const bar = element("span", "", "quality__bar");
        const fill = element("span", "", "quality__fill");
        fill.style.width = `${value}%`;
        bar.append(fill);
        row.append(element("span", label, "quality__label"), bar, element("span", String(value), "quality__value"));
        return row;
    }));
    const notes = [
        quality.corrected_words ? `${quality.corrected_words} word(s) corrected.` : "",
        ...quality.fallbacks.slice(0, 2),
    ].filter(Boolean);
    const note = $("quality-note");
    note.hidden = !notes.length;
    note.textContent = notes.join(" ");
    card.title = quality.ears.length ? `Heard by: ${quality.ears.join(", ")}` : "";
}
function level(value) {
    return value >= 85 ? "good" : value >= 60 ? "fair" : "low";
}
function renderTasks(items, hasReport) {
    $("tasks-block").hidden = !hasReport;
    const list = $("task-list");
    if (!items.length) {
        list.replaceChildren(element("li", "No action items were assigned in this meeting.", "task task--empty"));
        $("tasks-more").hidden = true;
        return;
    }
    list.replaceChildren(...items.slice(0, TASKS_SHOWN).map((item) => {
        const row = element("li", "", "task");
        const body = element("div");
        body.append(element("p", item.task, "task__text"));
        const meta = element("p", "", "task__meta");
        meta.append(element("span", item.owner));
        if (item.due_date || item.due_text)
            meta.append(element("span", `Due ${formatDue(item)}`, "task__due"));
        meta.append(element("span", item.priority, `prio prio--${item.priority}`));
        if (!item.verified)
            meta.append(element("span", "Please check", "task__check"));
        body.append(meta);
        row.append(avatar(item.owner), body);
        return row;
    }));
    const more = items.length - TASKS_SHOWN;
    $("tasks-more").hidden = more <= 0;
    $("tasks-more").textContent = `${more} more in the task tracker.`;
}
