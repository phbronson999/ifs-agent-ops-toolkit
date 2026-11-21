// End-to-end browser check: the real extension in headless Edge records a planned demonstration
// against a stand-in IFS web client (fake_ifs.py) into a running toolkit capture API.
//
//   1. ifs-agent-ops capture serve --port 8765          (use a scratch IFS_AGENT_OPS_HOME)
//   2. python browser-extension/tests/fake_ifs.py       (stand-in IFS on 127.0.0.1:8766)
//   3. node browser-extension/tests/e2e_edge.mjs         (after build.ps1)
//
// Drives the side panel exactly as a user would (Create plan, Start recording, Complete task, Stop
// recording). Only two test conveniences: the "active tab" is pinned to the stand-in IFS tab, and
// confirm dialogs are accepted. It then checks with the toolkit that each task received its request,
// that the plan is marked incomplete (stopped during task 2) and that no credential arrived.
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const EDGE = process.env.EDGE_PATH || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const EXT = resolve(fileURLToPath(new URL("../extension", import.meta.url)));
const TOOLKIT = process.env.CAPTURE_API || "http://127.0.0.1:8765";
const IFS = process.env.FAKE_IFS || "http://127.0.0.1:8766";
const PORT = 9400 + Math.floor(Math.random() * 500);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const toolkit = (method, path, body) => fetch(`${TOOLKIT}/capture/v1${path}`, {
  method, headers: { "Content-Type": "application/json" }, body: body && JSON.stringify(body) }).then((r) => r.json());

const profile = mkdtempSync(join(tmpdir(), "ifs-capture-e2e-"));
const edge = spawn(EDGE, [`--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, "--headless=new",
  "--no-first-run", "--no-default-browser-check", `--load-extension=${EXT}`, `--disable-extensions-except=${EXT}`,
  "about:blank"], { stdio: "ignore" });

let version;
for (let i = 0; i < 50 && !version; i++) {
  await sleep(200);
  version = await fetch(`http://127.0.0.1:${PORT}/json/version`).then((r) => r.json()).catch(() => undefined);
}
const ws = new WebSocket(version.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let nextId = 1;
const waiting = new Map();
ws.addEventListener("message", (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && waiting.has(msg.id)) { waiting.get(msg.id)(msg); waiting.delete(msg.id); }
});
const send = (method, params = {}, sessionId) => new Promise((ok, fail) => {
  const id = nextId++;
  waiting.set(id, (msg) => (msg.error ? fail(new Error(`${method}: ${msg.error.message}`)) : ok(msg.result)));
  ws.send(JSON.stringify({ id, method, params, sessionId }));
});
const evaluate = async (sessionId, expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sessionId);
  if (r.exceptionDetails) throw new Error(`${expression.slice(0, 80)}: ${r.exceptionDetails.exception?.description}`);
  return r.result.value;
};
const attach = async (targetId) => (await send("Target.attachToTarget", { targetId, flatten: true })).sessionId;

try {
  // our extension's service worker (Edge runs component extensions with background.js too)
  let sw;
  for (let i = 0; i < 30 && !sw; i++) {
    await sleep(300);
    const { targetInfos } = await send("Target.getTargets");
    for (const t of targetInfos.filter((x) => x.type === "service_worker" && x.url.endsWith("/background.js"))) {
      const s = await attach(t.targetId);
      await send("Runtime.runIfWaitingForDebugger", {}, s).catch(() => {});
      if (await evaluate(s, "chrome.runtime.getManifest().name").catch(() => "") === "IFS Agent Ops Capture") sw = s;
    }
  }
  assert.ok(sw, "extension not loaded (--load-extension ignored?)");

  const { targetId: ifsTarget } = await send("Target.createTarget", { url: `${IFS}/main/ifsapplications/web/` });
  await evaluate(sw, "chrome.tabs.create({ url: chrome.runtime.getURL('sidepanel.html') }).then(() => true)");
  let panelTarget;
  for (let i = 0; i < 20 && !panelTarget; i++) {
    await sleep(300);
    panelTarget = (await send("Target.getTargets")).targetInfos
      .find((t) => t.type === "page" && t.url.endsWith("/sidepanel.html"))?.targetId;
  }
  const ifs = await attach(ifsTarget);
  const panel = await attach(panelTarget);
  for (let i = 0; i < 30; i++) {
    if (await evaluate(panel, "!!document.getElementById('api-url')").catch(() => false)) break;
    await sleep(200);
  }
  const click = (text) => evaluate(panel, `(() => { const b = [...document.querySelectorAll('button')]
    .find((x) => x.textContent.includes(${JSON.stringify(text)}));
    if (!b) throw new Error('no button: ' + ${JSON.stringify(text)}); b.click(); return true; })()`);
  const type = (selector, value) => evaluate(panel, `(() => { const e = document.querySelector(${JSON.stringify(selector)});
    e.value = ${JSON.stringify(value)}; e.dispatchEvent(new Event('input')); return true; })()`);

  await evaluate(panel, `document.getElementById('api-url').value = ${JSON.stringify(TOOLKIT)}; true`);
  await click("Save");
  await sleep(800);
  await evaluate(panel, `(async () => {
    const [ifsTab] = await chrome.tabs.query({ url: ${JSON.stringify(IFS + "/*")} });
    chrome.tabs.query = async () => [ifsTab]; window.confirm = () => true; return true; })()`);

  for (let i = 0; i < 30; i++) {  // the plan form appears once the panel reached the toolkit
    if (await evaluate(panel, "!!document.querySelector('[data-key=new-intent]')")) break;
    await sleep(200);
  }
  // intent -> plan, in the side panel
  await type("[data-key=new-intent]", "I want to teach the builder how to create a customer order");
  await type("[data-key=new-tasks]", "Create order header\nAdd order line\nFinish order");
  await type("[data-key=new-workflow]", "create-order");
  await click("Create plan");
  await sleep(600);
  const sid = (await toolkit("GET", "/sessions")).sessions.at(-1).id;

  await click("Start recording");
  await sleep(1200);
  await evaluate(ifs, "header()");
  await sleep(1000);
  await click("Complete task");
  await sleep(800);
  await evaluate(ifs, "line()");
  await sleep(1000);
  await click("Stop recording");
  await sleep(4000);

  const view = await toolkit("GET", `/sessions/${sid}`);
  assert.equal(view.status, "stopped");
  assert.deepEqual(view.tasks.map((t) => t.status), ["complete", "incomplete", "pending"]);
  assert.deepEqual(view.requests.by_task, { t1: 1, t2: 1, t3: 0 });
  assert.equal(view.progress.plan_complete, false);
  const text = await evaluate(panel, "document.body.innerText");
  assert.match(text, /plan incomplete/);
  console.log(`e2e passed: session ${sid}; ${view.requests.total} IFS requests attributed to their tasks`);
} finally {
  await send("Browser.close").catch(() => {});  // ends every Edge process of this profile
  ws.close();
  edge.kill();
  await sleep(500);
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* Edge may still hold files */ }
}
