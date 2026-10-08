// HuRI playground: welcome -> conversation -> questionnaires -> thanks.
// Protocol with the gateway is documented in src/playground/gateway.py.
"use strict";

const $ = (id) => document.getElementById(id);

const ERRORS = {
  busy: "HuRI is talking with other visitors right now. Please try again in a minute.",
  bad_access_code: "That access code is not right.",
  huri_unavailable: "HuRI is offline at the moment. Please try again later.",
  huri_rejected: "HuRI could not start a conversation. Please try again later.",
  unknown_preset: "This mode is not available.",
};

const REJECTIONS = {
  rate_limited: "You are sending messages very quickly. Wait a moment and try again.",
  bad_data: "That message is empty or too long.",
};

const ENDINGS = {
  time_limit: "Time is up. Thank you for talking with HuRI!",
  huri_closed: "HuRI ended the conversation.",
  error: "The conversation stopped unexpectedly.",
};

const state = {
  config: null,
  ws: null,
  sessionId: null,
  answered: 0, // HuRI replies fully received
  waiting: false, // a question is in flight
  current: null, // <li> HuRI is writing into
  deadline: 0,
  timer: null,
  ended: false,
  step: 0, // index of the questionnaire on screen
  answers: {},
};

function show(screen) {
  document.body.dataset.screen = screen;
  for (const id of ["welcome", "chat", "survey", "end"]) {
    $(`screen-${id}`).hidden = id !== screen;
  }
  window.scrollTo(0, 0);
}

function showError(el, text) {
  el.textContent = text;
  el.hidden = !text;
}

// --- Welcome --------------------------------------------------------------

async function init() {
  try {
    const res = await fetch("/api/config");
    state.config = await res.json();
  } catch {
    showError($("start-error"), ERRORS.huri_unavailable);
    $("start-button").disabled = true;
    return;
  }
  const { config } = state;

  $("welcome-minutes").textContent = Math.round(config.max_seconds / 60);
  $("access-field").hidden = !config.access_code_required;

  const options = $("preset-options");
  config.presets.forEach((preset, i) => {
    const label = document.createElement("label");
    label.className = "check";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "preset";
    input.value = preset.id;
    input.checked = i === 0;
    const text = document.createElement("span");
    text.textContent = `${preset.label}: ${preset.description}`;
    label.append(input, text);
    options.append(label);
  });
  $("preset-field").hidden = config.presets.length < 2;

  $("start-form").addEventListener("submit", (e) => {
    e.preventDefault();
    start();
  });
}

function start() {
  showError($("start-error"), "");
  $("start-button").disabled = true;

  const preset = document.querySelector('input[name="preset"]:checked');
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/ws`);
  state.ws = ws;

  ws.addEventListener("open", () => {
    ws.send(
      JSON.stringify({
        type: "start",
        preset: preset ? preset.value : state.config.presets[0].id,
        access_code: $("access-code").value,
      }),
    );
  });
  ws.binaryType = "arraybuffer";
  // Browsers only let audio start from a user gesture: this click is it.
  speaker.unlock();
  ws.addEventListener("message", (e) => {
    if (typeof e.data === "string") onMessage(JSON.parse(e.data));
    else onBinary(e.data);
  });
  ws.addEventListener("close", onClose);
}

// --- Voice ----------------------------------------------------------------

// Plays HuRI's `audio.out` chunks back to back, in the order they arrive.
// avatar.js reads level() for Mouse-Man's talking cues, and contextTime() to
// play `motion` frames in step with the voice.
const speaker = {
  ctx: null,
  analyser: null,
  samples: null,
  next: 0, // AudioContext time at which the next chunk starts
  // [pts, AudioContext time] of each chunk of the current utterance. Kept
  // after the end marker: the last gesture chunk arrives after it.
  anchors: [],
  fresh: true, // the next chunk opens a new utterance

  unlock() {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.connect(this.ctx.destination);
      this.samples = new Float32Array(this.analyser.fftSize);
    }
    this.ctx.resume();
  },

  // Loudness (RMS) of what is playing right now; about 0.1 for speech.
  level() {
    if (!this.analyser) return 0;
    this.analyser.getFloatTimeDomainData(this.samples);
    let sum = 0;
    for (const s of this.samples) sum += s * s;
    return Math.sqrt(sum / this.samples.length);
  },

  play(sampleRate, samples, pts, end) {
    if (this.ctx && samples.length > 0) {
      const buffer = this.ctx.createBuffer(1, samples.length, sampleRate);
      buffer.copyToChannel(samples, 0);
      const source = this.ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(this.analyser);
      this.next = Math.max(this.next, this.ctx.currentTime + 0.05);
      source.start(this.next);

      if (this.fresh) this.anchors = [];
      this.fresh = false;
      this.anchors.push([pts, this.next]);
      this.next += buffer.duration;
    }
    if (end) this.fresh = true;
  },

  // AudioContext time at which utterance time `pts` is (or was) heard.
  contextTime(pts) {
    let anchor = null;
    for (const a of this.anchors) if (a[0] <= pts) anchor = a;
    return anchor && anchor[1] + (pts - anchor[0]);
  },
};
window.huriSpeaker = speaker;

// Binary frame: [u16 BE topic_len][topic][payload].
function onBinary(data) {
  const view = new DataView(data);
  const topicLen = view.getUint16(0);
  const topic = new TextDecoder().decode(new Uint8Array(data, 2, topicLen));
  const start = 2 + topicLen;
  if (state.ended) return;

  if (topic === "audio.out") {
    // [u32 BE sample_rate][u8 end][f64 BE pts][float32 samples]
    const sampleRate = view.getUint32(start);
    const end = view.getUint8(start + 4) === 1;
    const pts = view.getFloat64(start + 5);
    // slice() copies into a fresh, 4-byte aligned buffer for Float32Array.
    const samples = new Float32Array(data.slice(start + 13));
    speaker.play(sampleRate, samples, pts, end);
  } else if (topic === "motion") {
    // Parsed by avatar.js: [f64 BE pts][u32 BE fps][u32 BE frames][float32...]
    window.dispatchEvent(
      new CustomEvent("huri-motion", { detail: data.slice(start) }),
    );
  }
}

// --- Conversation ---------------------------------------------------------

function onMessage(msg) {
  if (msg.type === "ready") return onReady(msg);
  if (msg.type === "error") return onError(msg.reason);
  if (msg.type === "rejected") return onRejected(msg);
  if (msg.type === "ended") return onEnded(msg.reason);
  if (msg.topic === "token") return onToken(msg.data);
}

function onReady(msg) {
  state.sessionId = msg.session_id;
  // Tells avatar.js whether HuRI will send gestures this session.
  window.dispatchEvent(
    new CustomEvent("huri-session", { detail: { outbound: msg.outbound || [] } }),
  );
  const maxChars = msg.inbound.question && msg.inbound.question.max_chars;
  if (maxChars) $("question").maxLength = maxChars;

  state.deadline = Date.now() + msg.max_seconds * 1000;
  tick();
  state.timer = setInterval(tick, 1000);

  show("chat");
  $("question").focus();
}

function onError(reason) {
  state.ended = true;
  showError($("start-error"), ERRORS[reason] || "Something went wrong. Please try again.");
  $("start-button").disabled = false;
  show("welcome");
}

function onRejected(msg) {
  const notice = $("chat-notice");
  notice.textContent = REJECTIONS[msg.reason] || "That message was not sent.";
  notice.hidden = false;
  // The question never reached HuRI: drop its pending reply and let the
  // visitor try again.
  if (state.current) state.current.remove();
  state.current = null;
  setWaiting(false);
}

function onToken(token) {
  if (!state.current) state.current = addMessage("huri", "");
  if (token.end) {
    state.current.classList.remove("pending");
    state.current = null;
    state.answered += 1;
    $("finish").disabled = false;
    setWaiting(false);
    return;
  }
  state.current.textContent += token.text;
  scrollMessages();
}

function onEnded(reason) {
  state.ended = true;
  stopTimer();
  if (reason === "finished") return startSurvey();
  endConversation(ENDINGS[reason] || ENDINGS.error);
}

function onClose() {
  if (state.ended) return;
  state.ended = true;
  stopTimer();
  if (!state.sessionId) return onError("huri_unavailable");
  endConversation(ENDINGS.error);
}

// The conversation is over without the visitor pressing "finish": rate it if
// HuRI got to answer at least once, otherwise there is nothing to rate.
function endConversation(text) {
  setWaiting(true);
  $("finish").disabled = true;
  if (state.answered > 0) {
    $("chat-notice").textContent = `${text} Taking you to the questions…`;
    $("chat-notice").hidden = false;
    setTimeout(startSurvey, 2500);
  } else {
    showEnd("The conversation ended", `${text} Nothing to rate this time.`, true);
  }
}

function sendQuestion(e) {
  e.preventDefault();
  const text = $("question").value.trim();
  if (!text || state.waiting || state.ended) return;

  $("chat-notice").hidden = true;
  addMessage("you", text);
  state.current = addMessage("huri", "");
  state.current.classList.add("pending");
  $("question").value = "";
  setWaiting(true);

  state.ws.send(JSON.stringify({ topic: "question", data: { text } }));
}

function finish() {
  $("finish").disabled = true;
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "finish" }));
  } else {
    startSurvey();
  }
}

function setWaiting(waiting) {
  state.waiting = waiting;
  $("send").disabled = waiting;
  $("chat-status").textContent = waiting ? "HuRI is answering…" : "Your turn";
}

function addMessage(who, text) {
  const li = document.createElement("li");
  li.className = who;
  li.textContent = text;
  $("messages").append(li);
  scrollMessages();
  return li;
}

function scrollMessages() {
  const list = $("messages");
  list.scrollTop = list.scrollHeight;
}

function tick() {
  const left = Math.max(0, Math.round((state.deadline - Date.now()) / 1000));
  const min = Math.floor(left / 60);
  const sec = String(left % 60).padStart(2, "0");
  $("timer").textContent = `${min}:${sec} left`;
  $("timer").classList.toggle("low", left <= 30);
}

function stopTimer() {
  clearInterval(state.timer);
}

// --- Questionnaires -------------------------------------------------------

function startSurvey() {
  if (!$("screen-survey").hidden) return;
  if (state.ws) state.ws.close();
  state.step = 0;
  renderSurvey();
  show("survey");
}

function renderSurvey() {
  const qs = state.config.questionnaires;
  const q = qs[state.step];
  const last = state.step === qs.length - 1;

  $("survey-step").textContent = `Part ${state.step + 1} of ${qs.length}`;
  $("survey-title").textContent = q.title;
  $("comment-field").hidden = !last;
  $("survey-next").textContent = last ? "Send my answers" : "Next";
  showError($("survey-error"), "");

  const body = $("survey-body");
  body.replaceChildren();
  const differential = q.sections.some((s) => s.items.some((i) => i.low));
  body.className = differential ? "differential" : "likert";

  for (const section of q.sections) {
    const prompt = document.createElement("p");
    prompt.className = "section-prompt";
    prompt.textContent = section.prompt;
    body.append(prompt, scaleHead(q, differential));
    for (const item of section.items) body.append(itemRow(q, item, differential));
  }
}

function scaleHead(q, differential) {
  const head = document.createElement("div");
  head.className = "scale-head";
  head.setAttribute("aria-hidden", "true");
  const ends = document.createElement("span");
  ends.className = "ends";
  const low = document.createElement("span");
  const high = document.createElement("span");
  low.textContent = differential ? "" : q.scale.low || "";
  high.textContent = differential ? "" : q.scale.high || "";
  ends.append(low, high);
  if (differential) {
    head.append(document.createElement("span"), ends, document.createElement("span"));
  } else {
    head.append(document.createElement("span"), ends);
  }
  return head;
}

function itemRow(q, item, differential) {
  const row = document.createElement("fieldset");
  row.className = "item";
  row.dataset.item = item.id;

  const legend = document.createElement("legend");
  legend.className = "visually-hidden";
  legend.textContent = differential
    ? `From ${item.low} (1) to ${item.high} (${q.scale.points})`
    : `${item.label}, from ${q.scale.low} (1) to ${q.scale.high} (${q.scale.points})`;
  row.append(legend);

  const points = document.createElement("div");
  points.className = "points";
  const previous = (state.answers[q.id] || {})[item.id];
  for (let value = 1; value <= q.scale.points; value++) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "radio";
    input.name = `${q.id}.${item.id}`;
    input.value = String(value);
    input.checked = previous === value;
    input.addEventListener("change", () => row.classList.remove("missing"));
    const n = document.createElement("span");
    n.textContent = String(value);
    label.append(input, n);
    points.append(label);
  }

  if (differential) {
    const low = document.createElement("span");
    low.className = "low";
    low.textContent = item.low;
    const high = document.createElement("span");
    high.className = "high";
    high.textContent = item.high;
    row.append(low, points, high);
  } else {
    const name = document.createElement("span");
    name.textContent = item.label;
    row.append(name, points);
  }
  return row;
}

async function nextSurveyStep(e) {
  e.preventDefault();
  const qs = state.config.questionnaires;
  const q = qs[state.step];

  const answers = {};
  let firstMissing = null;
  for (const row of document.querySelectorAll("#survey-body .item")) {
    const checked = row.querySelector("input:checked");
    if (checked) {
      answers[row.dataset.item] = Number(checked.value);
    } else {
      row.classList.add("missing");
      firstMissing = firstMissing || row;
    }
  }
  if (firstMissing) {
    showError($("survey-error"), "Please answer every line before going on.");
    firstMissing.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  state.answers[q.id] = answers;

  if (state.step < qs.length - 1) {
    state.step += 1;
    renderSurvey();
    window.scrollTo(0, 0);
    return;
  }
  await submit();
}

async function submit() {
  $("survey-next").disabled = true;
  showError($("survey-error"), "");
  try {
    const res = await fetch("/api/responses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.sessionId,
        answers: state.answers,
        comment: $("comment").value,
      }),
    });
    if (res.status === 201 || res.status === 409) {
      showEnd("Thank you!", "Your answers were saved. You can close this page.", false);
      return;
    }
    const body = await res.json().catch(() => ({}));
    showError($("survey-error"), body.error || "Your answers could not be saved.");
  } catch {
    showError($("survey-error"), "Your answers could not be sent. Check your connection and try again.");
  }
  $("survey-next").disabled = false;
}

function showEnd(title, text, canRestart) {
  $("end-title").textContent = title;
  $("end-text").textContent = text;
  $("restart").hidden = !canRestart;
  show("end");
}

// --- Wiring ---------------------------------------------------------------

$("composer").addEventListener("submit", sendQuestion);
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) sendQuestion(e);
});
$("finish").addEventListener("click", finish);
$("survey-form").addEventListener("submit", nextSurveyStep);
$("restart").addEventListener("click", () => location.reload());

init();
