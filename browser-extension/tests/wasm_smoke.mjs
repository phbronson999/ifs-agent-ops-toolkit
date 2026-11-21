// Smoke test of the built WASM bindings (extension/pkg) as the side panel uses them.
// Run after build.ps1:  node browser-extension/tests/wasm_smoke.mjs
import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import { initSync, CaptureCore, commandPayload, originOf, protocolVersion }
  from "../extension/pkg/ifs_capture_core.js";

initSync({ module: await readFile(new URL("../extension/pkg/ifs_capture_core_bg.wasm", import.meta.url)) });

assert.equal(protocolVersion(), 1);
assert.equal(originOf("https://IFS.example:8443/main/ifsapplications/web/page"), "https://ifs.example:8443");
assert.equal(originOf("chrome://extensions"), undefined);

const core = new CaptureCore("https://ifs.example");
const svc = "https://ifs.example/main/ifsapplications/projection/v1/X.svc";
let actions = JSON.parse(core.on_event("Network.requestWillBeSent", JSON.stringify({
  requestId: "1", type: "XHR", timestamp: 10, wallTime: 1000,
  request: { url: `${svc}/OrderSet`, method: "POST", hasPostData: true,
             headers: { Authorization: "Bearer x", "Content-Type": "application/json" } },
})));
assert.deepEqual(actions, [{ action: "fetch_post_data", request_id: "1" }]);
core.on_post_data("1", '{"CustomerNo":"C1"}');
core.on_event("Network.responseReceived", JSON.stringify({ requestId: "1", timestamp: 10.5,
  response: { status: 201, statusText: "Created", mimeType: "application/json", headers: { "Set-Cookie": "s" } } }));
actions = JSON.parse(core.on_event("Network.loadingFinished", JSON.stringify({ requestId: "1", timestamp: 10.6 })));
assert.deepEqual(actions, [{ action: "fetch_body", request_id: "1" }]);
core.on_body("1", '{"OrderNo":"1"}', false);
core.on_event("Network.requestWillBeSent", JSON.stringify({ requestId: "2", type: "Script", timestamp: 11,
  wallTime: 1001, request: { url: "https://ifs.example/app.js", method: "GET", headers: {} } }));

const batch = JSON.parse(core.take_batch(1_000_000));
assert.equal(core.take_batch(1_000_000), undefined);
assert.deepEqual(batch.events.map((e) => e.type), ["request", "response"]);
assert.equal(batch.events[0].post_data, '{"CustomerNo":"C1"}');
assert.equal(batch.events[0].headers.Authorization, undefined);
assert.equal(batch.events[1].ts, 1000.6);
assert.equal(batch.events[1].headers["Set-Cookie"], undefined);
assert.equal(JSON.parse(core.stats()).ignored, 1);
assert.deepEqual(JSON.parse(commandPayload(5.5, undefined)), { ts: 5.5 });
assert.deepEqual(JSON.parse(commandPayload(5.5, "t2")), { ts: 5.5, task_id: "t2" });
console.log("wasm smoke test passed");
