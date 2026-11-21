# IFS Agent Ops capture extension

This browser extension (Manifest V3, for Edge or Chrome) records a planned IFS Cloud demonstration
task by task. It is the browser half of guided capture (roadmap §1.1, Phase 9a). The toolkit is the
other half and stays the authority. It holds:
- the session
- the intent
- the ordered recording plan
- the task boundaries
- the recorded events

The extension is a capture-and-control client.

```text
side panel (sidepanel.js) ──HTTP──> toolkit /capture/v1  (ifs-agent-ops-mcp, or ifs-agent-ops capture serve)
     │ chrome.debugger (Network.*)
     ▼
Rust/WASM core (core/) ── filters to the IFS origin, XHR/fetch only; removes credential headers;
                          orders events; converts CDP times to wall-clock; builds batches
```

- **`core/`**: the Rust crate `ifs-capture-core`. It is pure logic with `cargo test` unit tests; the
  `wasm` feature adds the wasm-bindgen surface (`src/wasm.rs`).
- **`extension/`**: the manifest, background worker, side panel UI and the thin JS layer. The build
  output goes to `extension/pkg/`, which is not committed.

## Build

```powershell
rustup target add wasm32-unknown-unknown
cargo install wasm-bindgen-cli --version 0.2.100 --locked
./build.ps1
```

## Load

1. Open `edge://extensions` (or `chrome://extensions`) and turn on **Developer mode**.
2. Click **Load unpacked** and select `browser-extension/extension`.
3. Click the toolbar button to open the side panel.

## Use

1. Start the toolkit: `ifs-agent-ops-mcp` (Claude can then plan with you), or `ifs-agent-ops capture serve`.
2. Create the plan:
   - in Claude ("I want to teach the builder how to create a customer order"), or
   - in the side panel, or
   - with `ifs-agent-ops capture new`.
3. Open the IFS web client in the active tab. In the side panel, select the session and press
   **Start recording**.
4. Do the current task in IFS, then press **Complete task**. You can also skip a task, or add one.
5. Press **Stop recording**. Then import the session:
   `ifs-agent-ops capture import <session> --package <package>`.

## Tests

```powershell
cd core; cargo test; cd ..                       # capture core (pure Rust)
./build.ps1                                       # also runs tests/wasm_smoke.mjs on the built bindings
# real browser, headless Edge (3 terminals, scratch toolkit home):
$env:IFS_AGENT_OPS_HOME = "$env:TEMP\ifs-capture-test"; ifs-agent-ops capture serve --port 8765
python tests/fake_ifs.py
node tests/e2e_edge.mjs
```

## What it records

- Requests to the IFS origin of the recorded tab, and their responses. Only XHR and fetch: no
  scripts, styles, images or other sites.
- Request and response bodies, including server-trace payloads, because analysis needs the data
  flow.

## What never leaves the browser

The `Authorization`, `Cookie`, `Set-Cookie` and `Proxy-Authorization` headers.

## Where recordings live

Raw recordings stay on this machine, in the toolkit home under `captures/`. A package receives only
the sanitized recording, produced by the existing `record import` boundary.

## While recording

The browser shows a notice that the extension is debugging the tab. Keep the side panel open while
recording. If you close it, reopen it and press **Resume capture**. The session and its task
boundaries are kept either way.
