/** Setup page: 3-step first-run wizard, reopened later from the gear icon as Settings. */
import { checkAuth, checkHealth } from "../shared/api.js";
import { backendConfig, configProblems, getPreferences, microphoneAllowed, savePreferences, serverUrl } from "../shared/settings.js";
const $ = (id) => {
    const element = document.getElementById(id);
    if (!element)
        throw new Error(`Missing #${id}`);
    return element;
};
const state = {
    step: 1,
    setupComplete: false,
    serverOk: false,
    checking: false,
    meterStream: null,
    meterFrame: 0,
};
document.addEventListener("DOMContentLoaded", () => void init());
async function init() {
    const preferences = await getPreferences();
    state.setupComplete = preferences.setupComplete;
    $("display-name").value = preferences.displayName;
    $("server-url").textContent = serverUrl();
    updateNameDemo();
    for (const button of document.querySelectorAll("[data-goto]")) {
        button.addEventListener("click", () => goTo(Number(button.dataset.goto)));
    }
    $("recheck-btn").addEventListener("click", () => void runChecks());
    $("next-1").addEventListener("click", () => goTo(2));
    $("display-name").addEventListener("input", updateNameDemo);
    $("display-name").addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !$("next-2").disabled)
            void saveName();
    });
    $("next-2").addEventListener("click", () => void saveName());
    $("mic-btn").addEventListener("click", () => void requestMic());
    $("finish-btn").addEventListener("click", () => void finish());
    $("skip-mic").addEventListener("click", () => void finish(false));
    $("close-btn").addEventListener("click", () => window.close());
    goTo(state.setupComplete ? 4 : 1);
    void runChecks();
    await refreshMic();
}
/* ---------- navigation ---------- */
function goTo(step) {
    if (step === 3 || step === 4)
        void refreshMic();
    if (step !== 3)
        stopMeter();
    state.step = step;
    for (const card of document.querySelectorAll("[data-step]")) {
        card.hidden = Number(card.dataset.step) !== step;
    }
    document.querySelectorAll("#stepper li").forEach((item, i) => {
        const number = (i + 1);
        item.dataset.state = number === step ? "current" : number < step || state.setupComplete ? "done" : "todo";
        const button = item.querySelector("button");
        if (button)
            button.disabled = !state.setupComplete && number > step;
    });
    if (step === 2)
        $("display-name").focus();
}
/* ---------- step 1: server checks ---------- */
async function runChecks() {
    if (state.checking)
        return;
    state.checking = true;
    state.serverOk = false;
    $("next-1").disabled = true;
    for (const id of ["check-config", "check-server", "check-token", "check-stt", "check-notes"])
        setCheck(id, "idle", "");
    try {
        setCheck("check-config", "running", "Reading config.js…");
        const problems = configProblems();
        if (problems.length)
            return setCheck("check-config", "fail", `${problems.join(" ")} Ask whoever shared MeetMind.`);
        setCheck("check-config", "ok", serverUrl());
        setCheck("check-server", "running", "Contacting the server (waking it if asleep)…");
        if (!(await checkHealth(serverUrl()))) {
            return setCheck("check-server", "fail", "No answer. Check server.url in config.js and that the backend is running.");
        }
        setCheck("check-server", "ok", "Online");
        setCheck("check-token", "running", "Checking the app token…");
        const auth = await checkAuth(await backendConfig());
        if (auth.speechToTextReady === undefined)
            return setCheck("check-token", "fail", auth.error ?? "Rejected.");
        setCheck("check-token", "ok", "Accepted");
        if (!auth.speechToTextReady) {
            return setCheck("check-stt", "fail", "The server has no Deepgram or Groq key yet. Ask your admin.");
        }
        const engine = auth.speechEngine ? `${auth.speechEngine}. ` : "";
        setCheck("check-stt", "ok", `${engine}${Math.floor(auth.minutesLeftToday ?? 0)} minutes of transcription left today`);
        // Notes are optional: recording and transcripts still work without them.
        if (auth.summaryReady)
            setCheck("check-notes", "ok", "Summary, minutes and tasks will be created after each meeting");
        else
            setCheck("check-notes", "warn", "The server has no Gemini key, so you get transcripts only. Ask your admin.");
        state.serverOk = true;
        $("next-1").disabled = false;
    }
    finally {
        state.checking = false;
    }
}
function setCheck(id, value, detail) {
    const row = $(id);
    row.dataset.state = value;
    const small = row.querySelector("small");
    if (small)
        small.textContent = detail;
}
/* ---------- step 2: name ---------- */
function updateNameDemo() {
    const name = $("display-name").value.trim();
    $("demo-name").textContent = name || "Your name";
    $("next-2").disabled = name.length < 2;
}
async function saveName() {
    await savePreferences({ displayName: $("display-name").value });
    goTo(3);
}
/* ---------- step 3: microphone ---------- */
async function requestMic() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        for (const track of stream.getTracks())
            track.stop();
    }
    catch {
        // refreshMic() explains what to do next.
    }
    await refreshMic();
}
async function refreshMic() {
    let permission = "prompt";
    try {
        permission = (await navigator.permissions.query({ name: "microphone" })).state;
    }
    catch {
        permission = (await microphoneAllowed()) ? "granted" : "prompt";
    }
    const box = $("mic-box");
    box.dataset.state = permission;
    $("mic-btn").hidden = permission === "granted";
    $("mic-meter").hidden = permission !== "granted";
    const text = {
        granted: ["Microphone allowed", "Say something: the bar should move."],
        prompt: ["Microphone not allowed yet", 'Click Allow, then choose "Allow" in Chrome\'s prompt.'],
        denied: ["Microphone blocked", "Click the lock icon in the address bar, set Microphone to Allow, then reload this page."],
    };
    $("mic-title").textContent = text[permission][0];
    $("mic-text").textContent = text[permission][1];
    if (permission === "granted" && state.step === 3)
        void startMeter();
}
/** Live level bar so the user can see the right microphone is working. */
async function startMeter() {
    if (state.meterStream)
        return;
    try {
        state.meterStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    }
    catch {
        return;
    }
    const context = new AudioContext();
    const analyser = context.createAnalyser();
    analyser.fftSize = 512;
    context.createMediaStreamSource(state.meterStream).connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const draw = () => {
        if (!state.meterStream) {
            void context.close();
            return;
        }
        analyser.getFloatTimeDomainData(samples);
        let sum = 0;
        for (const sample of samples)
            sum += sample * sample;
        const levelValue = Math.min(1, Math.sqrt(sum / samples.length) * 5);
        $("mic-level").style.width = `${Math.round(levelValue * 100)}%`;
        state.meterFrame = requestAnimationFrame(draw);
    };
    draw();
}
function stopMeter() {
    cancelAnimationFrame(state.meterFrame);
    for (const track of state.meterStream?.getTracks() ?? [])
        track.stop();
    state.meterStream = null;
}
/* ---------- finish ---------- */
async function finish(includeMic = true) {
    await savePreferences({
        displayName: $("display-name").value,
        includeMic: includeMic && (await microphoneAllowed()),
        setupComplete: true,
    });
    state.setupComplete = true;
    stopMeter();
    goTo(4);
}
window.addEventListener("pagehide", stopMeter);
