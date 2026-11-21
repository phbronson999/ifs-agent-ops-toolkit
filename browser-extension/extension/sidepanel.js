// IFS Agent Ops capture side panel: the recording UI and the thin JS layer around the Rust core.
//
// The toolkit owns the session (intent, plan, task boundaries, events). This panel:
//  - shows and edits the plan through the toolkit's local capture API (/capture/v1)
//  - attaches chrome.debugger to the IFS tab while recording and feeds Network.* events to the
//    Rust core (pkg/ifs_capture_core.js), which filters, strips credentials and orders them
//  - sends events and task commands through ONE ordered queue, so a "complete" click lands after
//    every request sent before it and before every request sent after it
//  - polls the session, so changes made from Claude (MCP) or the CLI appear here too

import init, { CaptureCore, commandPayload, originOf } from "./pkg/ifs_capture_core.js";

const PROTOCOL = 1;
const BATCH_BYTES = 4 * 1024 * 1024;
const POLL_MS = 1500;
const FLUSH_MS = 500;

const state = {
  api: "http://127.0.0.1:8000",
  online: false,
  sessions: [],
  selected: null,   // session id
  session: null,    // session view from the toolkit
  capture: null,    // { tabId, core, origin } while this panel is capturing
  queue: Promise.resolve(),
  drafts: {},       // text typed into inputs survives the periodic re-render
};

// ── toolkit API ─────────────────────────────────────────────────────────────

async function api(method, path, body) {
  const res = await fetch(`${state.api}/capture/v1${path}`, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/** Run fn after everything already queued (events and commands stay in click/send order). */
function enqueue(fn) {
  const next = state.queue.then(fn, fn);
  state.queue = next.catch(() => {});
  return next;
}

function flushEvents() {
  return enqueue(async () => {
    const cap = state.capture;
    if (!cap) return;
    let batch;
    while ((batch = cap.core.take_batch(BATCH_BYTES)) !== undefined) {
      await api("POST", `/sessions/${state.selected}/events`, batch);
    }
  });
}

async function command(action, extra = {}) {
  const ts = Date.now() / 1000; // the moment of the click is the task boundary
  if (state.capture && action === "stop") state.capture.core.flush_requests();
  await flushEvents();
  const body = { ...JSON.parse(commandPayload(ts, extra.task_id)), ...extra };
  state.session = await enqueue(() => api("POST", `/sessions/${state.selected}/${action}`, body));
  render();
}

// ── capture (chrome.debugger) ───────────────────────────────────────────────

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab;
}

async function attach(tab, origin) {
  const target = { tabId: tab.id };
  await chrome.debugger.attach(target, "1.3");
  await chrome.debugger.sendCommand(target, "Network.enable", {
    maxTotalBufferSize: 100_000_000, maxResourceBufferSize: 20_000_000, maxPostDataSize: 65_536,
  });
  state.capture = { tabId: tab.id, core: new CaptureCore(origin), origin };
  await chrome.storage.session.set({ capturing: { session: state.selected, tabId: tab.id } });
}

async function detach() {
  const cap = state.capture;
  if (!cap) return;
  // give in-flight responses a moment, then send what arrived and let go of the tab
  const deadline = Date.now() + 3000;
  while (cap.core.pending() > 0 && Date.now() < deadline) await new Promise((r) => setTimeout(r, 200));
  await flushEvents();
  state.capture = null;
  await chrome.storage.session.remove("capturing");
  await chrome.debugger.detach({ tabId: cap.tabId }).catch(() => {});
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  const cap = state.capture;
  if (!cap || source.tabId !== cap.tabId || !method.startsWith("Network.")) return;
  let actions = [];
  try {
    actions = JSON.parse(cap.core.on_event(method, JSON.stringify(params)));
  } catch (e) {
    console.warn("capture core rejected an event", method, e);
  }
  for (const a of actions) {
    const target = { tabId: cap.tabId };
    if (a.action === "fetch_body") {
      chrome.debugger.sendCommand(target, "Network.getResponseBody", { requestId: a.request_id })
        .then((r) => cap.core.on_body(a.request_id, r.body, !!r.base64Encoded))
        .catch(() => cap.core.on_body(a.request_id, undefined, false));
    } else if (a.action === "fetch_post_data") {
      chrome.debugger.sendCommand(target, "Network.getRequestPostData", { requestId: a.request_id })
        .then((r) => cap.core.on_post_data(a.request_id, r.postData))
        .catch(() => cap.core.on_post_data(a.request_id, undefined));
    }
  }
});

chrome.debugger.onDetach.addListener((source, reason) => {
  if (state.capture && source.tabId === state.capture.tabId) {
    state.capture = null;
    chrome.storage.session.remove("capturing");
    show(`Capture detached from the IFS tab (${reason}). Requests from now on are not recorded; ` +
         "use Resume capture to continue.", true);
    render();
  }
});

// ── actions ─────────────────────────────────────────────────────────────────

async function startRecording() {
  const tab = await activeTab();
  const origin = tab && tab.url ? originOf(tab.url) : undefined;
  if (!origin) return show("Open the IFS web client in the active tab first, then press Start recording.", true);
  if (!confirm(`Record IFS traffic from ${origin} in the current tab?\n\nThe browser shows a debugging notice ` +
               "while recording. Credentials are never recorded.")) return;
  state.session = await api("POST", `/sessions/${state.selected}/start`,
                            { ifs_origin: origin, ts: Date.now() / 1000 });
  await attach(tab, origin);
  show(`Recording ${origin}. Perform "${state.session.current_task.title}" in IFS, then press Complete.`);
  render();
}

async function resumeCapture() {
  const tab = await activeTab();
  const origin = tab && tab.url ? originOf(tab.url) : undefined;
  if (origin !== state.session.ifs_origin) {
    return show(`Switch to the IFS tab (${state.session.ifs_origin}) and press Resume capture again.`, true);
  }
  await attach(tab, origin);
  show("Capture resumed.");
  render();
}

async function stopRecording() {
  if (!confirm("Stop the recording? The session, task boundaries and events are kept.")) return;
  await command("stop");
  await detach();
  render();
}

// ── polling ─────────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const health = await api("GET", "/health");
    state.online = health.ok && health.protocol === PROTOCOL;
    state.sessions = (await api("GET", "/sessions")).sessions;
    if (state.selected) state.session = await api("GET", `/sessions/${state.selected}`);
    if (state.capture && state.session && state.session.status === "stopped") {
      await detach(); // stopped from Claude or the CLI
      show("The recording was stopped.");
    }
  } catch (e) {
    state.online = false;
  }
  render();
}

// ── rendering ───────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);
const MARKS = { complete: "✓", active: "▶", pending: "○", skipped: "–", incomplete: "!" };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "onclick") node.addEventListener("click", (e) => guard(() => v(e)));
    else if (k === "draft") {
      if (state.drafts[v] !== undefined) node.value = state.drafts[v];
      node.addEventListener("input", () => { state.drafts[v] = node.value; });
    } else if (k === "class") node.className = v;
    else if (v !== false && v !== undefined) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) node.append(c);
  if (attrs.draft && state.drafts[attrs.draft] !== undefined) node.value = state.drafts[attrs.draft];
  return node;
}

function takeDraft(key) {
  delete state.drafts[key];
}

function show(text, error = false) {
  const m = $("message");
  m.hidden = !text;
  m.textContent = text || "";
  m.className = "message" + (error ? " error" : "");
}

async function guard(fn) {
  try {
    await fn();
  } catch (e) {
    show(String(e.message || e), true);
  }
}

function render() {
  $("connection").textContent = state.online ? `toolkit connected (${state.api})`
    : `toolkit not reachable at ${state.api}: start ifs-agent-ops-mcp or ifs-agent-ops capture serve`;
  const app = $("app");
  const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null;
  app.replaceChildren(state.session ? sessionView(state.session) : pickerView());
  if (focused) {
    const again = app.querySelector(`[data-key="${focused}"]`);
    if (again) { again.focus(); if (again.value !== undefined && again.setSelectionRange) {
      try { again.setSelectionRange(again.value.length, again.value.length); } catch (_) { /* select */ } } }
  }
}

function pickerView() {
  const open = state.sessions.filter((s) => s.status !== "stopped");
  const done = state.sessions.filter((s) => s.status === "stopped").slice(-5).reverse();
  const pick = (s) => el("button", { onclick: async () => {
    state.selected = s.id;
    state.session = await api("GET", `/sessions/${s.id}`);
    render();
  } }, `${s.intent} `, el("span", { class: "badge" }, `${s.status} ${s.progress.done}/${s.progress.total}`));
  const intent = el("textarea", { rows: 2, draft: "new-intent", "data-key": "new-intent",
    placeholder: "I want to teach the builder how to create a customer order" });
  const workflow = el("input", { draft: "new-workflow", "data-key": "new-workflow",
    placeholder: "workflow folder, e.g. create-customer-order" });
  const tasks = el("textarea", { rows: 4, draft: "new-tasks", "data-key": "new-tasks", placeholder: "One task per line, in order:\nCreate order header\nAdd order line\nAdd charge\nFinish order" });
  return el("div", {},
    el("h2", {}, "New recording"),
    el("label", {}, "Intent", intent),
    el("label", {}, "Recording plan", tasks),
    el("label", {}, "Workflow (optional)", workflow),
    el("div", { class: "row" }, el("button", { class: "primary", disabled: !state.online, onclick: async () => {
      const titles = tasks.value.split("\n").map((t) => t.trim()).filter(Boolean);
      const s = await api("POST", "/sessions", { intent: intent.value, tasks: titles, workflow: workflow.value });
      ["new-intent", "new-workflow", "new-tasks"].forEach(takeDraft);
      state.selected = s.id;
      state.session = s;
      show("Review the plan, open the IFS web client in this window, then press Start recording.");
      render();
    } }, "Create plan")),
    el("p", { class: "muted" }, "Or ask Claude: \"I want to teach the builder how to ...\". The plan it creates appears here."),
    open.length ? el("h2", {}, "Open sessions") : null,
    el("div", { class: "sessions" }, open.map(pick)),
    done.length ? el("h2", {}, "Recently stopped") : null,
    el("div", { class: "sessions" }, done.map(pick)));
}

function sessionView(s) {
  const p = s.progress;
  const recording = s.status === "recording";
  const planning = s.status === "planning";
  const badge = recording ? el("span", { class: "badge recording" }, "● recording")
    : planning ? el("span", { class: "badge" }, "planning")
    : el("span", { class: "badge " + (p.plan_complete ? "complete" : "incomplete") },
         p.plan_complete ? "plan complete" : "plan incomplete");
  const counts = (s.requests || {}).by_task || {};

  const intentKey = `intent-${s.id}`;
  const intentBox = el("textarea", { rows: 2, draft: intentKey, "data-key": intentKey });
  if (state.drafts[intentKey] === undefined) intentBox.value = s.intent;
  const header = el("div", { class: "card" },
    el("div", { class: "row" }, el("button", { class: "small", onclick: () => {
      state.selected = null; state.session = null; render();
    } }, "← sessions"), badge),
    planning || recording
      ? el("div", {}, intentBox, el("div", { class: "row" }, el("button", { class: "small", onclick: async () => {
          state.session = await api("POST", `/sessions/${s.id}/intent`, { intent: intentBox.value });
          takeDraft(intentKey);
          render();
        } }, "Save intent")))
      : el("div", { class: "intent" }, s.intent),
    el("div", { class: "muted" }, `${s.id}${s.workflow ? " · " + s.workflow : ""}`),
    el("progress", { max: Math.max(p.total, 1), value: p.done }),
    el("div", { class: "muted" }, `${p.done} of ${p.total} tasks done` +
       (s.requests ? ` · ${s.requests.total} IFS requests` : "")));

  const list = el("ol", { class: "tasks" }, s.tasks.map((t, i) => el("li", { class: t.status },
    el("span", { class: "mark" }, MARKS[t.status]),
    el("span", { class: "title" }, `${i + 1}. ${t.title}`,
       t.added_during_recording ? el("span", { class: "muted" }, " (added while recording)") : null),
    !planning ? el("span", { class: "count" }, `${counts[t.id] || 0} req`) : null,
    t.status === "pending" && (planning || recording) ? [
      el("button", { class: "small", title: "Move up", onclick: () => move(t, i) }, "↑"),
      el("button", { class: "small", title: "Move down", onclick: () => move(t, i + 2) }, "↓"),
      el("button", { class: "small", title: "Rename", onclick: async () => {
        const title = prompt("Task title", t.title);
        if (title) { state.session = await api("PATCH", `/sessions/${s.id}/tasks/${t.id}`, { title }); render(); }
      } }, "✎"),
      el("button", { class: "small", title: "Remove", onclick: async () => {
        state.session = await api("DELETE", `/sessions/${s.id}/tasks/${t.id}`); render();
      } }, "✕"),
    ] : null)));

  const newTask = el("input", { draft: `task-${s.id}`, "data-key": `task-${s.id}`, placeholder: recording ? "Another business action to demonstrate" : "Add a task" });
  const where = el("select", {}, el("option", { value: "next" }, "after current"), el("option", { value: "end" }, "at end"));
  const addRow = planning || recording ? el("div", { class: "row" }, newTask, recording ? where : null,
    el("button", { onclick: async () => {
      if (!newTask.value.trim()) return;
      state.session = await api("POST", `/sessions/${s.id}/tasks`, {
        title: newTask.value, position: recording ? where.value : "end", ts: Date.now() / 1000 });
      takeDraft(`task-${s.id}`);
      render();
    } }, "Add task")) : null;

  const controls = [];
  if (planning) {
    controls.push(el("div", { class: "row" }, el("button", { class: "primary", disabled: !s.tasks.length || !state.online,
      onclick: startRecording }, "Start recording")),
      el("p", { class: "muted" }, "Open the IFS web client in the active tab of this window first."));
  } else if (recording) {
    const cur = s.current_task;
    controls.push(el("div", { class: "card" },
      el("div", { class: "muted" }, cur ? "Current task" : "Every planned task is done"),
      el("div", { class: "current" }, cur ? cur.title : "Add another task or stop the recording."),
      el("div", { class: "row" },
        el("button", { class: "primary", disabled: !cur, onclick: () => command("complete", { task_id: cur.id }) },
           "Complete task"),
        el("button", { disabled: !cur, onclick: () => command("skip", { task_id: cur.id }) }, "Skip task"),
        el("button", { class: "danger", onclick: stopRecording }, "Stop recording")),
      state.capture ? el("div", { class: "muted" }, `capturing ${state.capture.origin}`)
        : el("div", { class: "row" }, el("span", { class: "muted" }, "Not capturing in this panel."),
             el("button", { onclick: resumeCapture }, "Resume capture"))));
  } else {
    controls.push(el("div", { class: "card" },
      el("div", {}, p.plan_complete ? "Every task was completed or skipped."
        : "Not every task was completed: the recording is kept and marked incomplete."),
      el("div", { class: "muted" }, s.next_step)));
  }
  return el("div", {}, header, controls, el("h2", {}, "Recording plan"), list, addRow);
}

async function move(task, position) {
  state.session = await api("POST", `/sessions/${state.selected}/tasks/${task.id}/move`, { position });
  render();
}

// ── boot ────────────────────────────────────────────────────────────────────

async function main() {
  await init();
  const saved = await chrome.storage.local.get(["api"]);
  if (saved.api) state.api = saved.api;
  $("api-url").value = state.api;
  $("save-settings").addEventListener("click", () => guard(async () => {
    state.api = $("api-url").value.replace(/\/+$/, "");
    await chrome.storage.local.set({ api: state.api });
    await refresh();
  }));
  const { capturing } = await chrome.storage.session.get("capturing");
  if (capturing) {
    state.selected = capturing.session; // the panel was reopened during a recording
    show("The side panel was reopened during a recording: press Resume capture in the IFS tab.", true);
    await chrome.debugger.detach({ tabId: capturing.tabId }).catch(() => {});
    await chrome.storage.session.remove("capturing");
  }
  await refresh();
  setInterval(refresh, POLL_MS);
  setInterval(() => { if (state.capture) flushEvents().catch((e) => show(String(e.message || e), true)); }, FLUSH_MS);
}

main().catch((e) => show(String(e.message || e), true));
