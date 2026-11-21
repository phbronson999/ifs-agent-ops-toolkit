//! Capture core of the IFS Agent Ops browser extension.
//!
//! The side panel attaches `chrome.debugger` to the IFS tab and feeds every `Network.*` CDP event
//! to a [`Recorder`]. The recorder turns them into the toolkit's capture events (protocol v1):
//!
//! - `request`  `{type, seq, request_id, ts, method, url, headers, resource_type, post_data?}`
//! - `response` `{type, seq, request_id, ts, status, status_text, headers, mime_type, body?, base64}`
//! - `failed`   `{type, seq, request_id, ts, error_text}`
//!
//! Rules enforced here, before anything leaves the browser:
//! - only XHR/Fetch requests to the IFS origin being recorded (no static assets, no other sites)
//! - credential headers (`Authorization`, `Cookie`, `Set-Cookie`, `Proxy-Authorization`) are removed
//! - `seq` follows the order requests were sent; `ts` is wall-clock seconds (CDP `wallTime`, and the
//!   monotonic CDP timestamps converted with the same offset), which is what the toolkit uses to
//!   attribute each request to the plan task that was current when it was sent
//! - a request is always emitted before its response (the toolkit drops responses to unknown requests)
//! - redirects keep every hop (`<id>`, `<id>:1`, ...)
//!
//! The recorder never performs I/O. It returns [`Action`]s (fetch a body, fetch post data) that the
//! JS layer performs through `chrome.debugger.sendCommand`, and builds the JSON payloads JS posts
//! to the toolkit.

use serde_json::{json, Map, Value};
use std::collections::HashMap;

#[cfg(feature = "wasm")]
pub mod wasm;

pub const PROTOCOL_VERSION: u32 = 1;
pub const CREDENTIAL_HEADERS: [&str; 4] = ["authorization", "proxy-authorization", "cookie", "set-cookie"];
pub const CAPTURED_TYPES: [&str; 2] = ["XHR", "Fetch"];
pub const MAX_BODY_CHARS: usize = 8_000_000;

/// Work the JS layer must do for the recorder (CDP commands).
#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    FetchBody(String),
    FetchPostData(String),
}

impl Action {
    pub fn to_json(&self) -> Value {
        match self {
            Action::FetchBody(id) => json!({"action": "fetch_body", "request_id": id}),
            Action::FetchPostData(id) => json!({"action": "fetch_post_data", "request_id": id}),
        }
    }
}

#[derive(Debug)]
struct Pending {
    hop_id: String,
    seq: u64,
    request: Option<Value>, // not yet emitted (waiting for post data)
    emitted: bool,
    response: Option<Map<String, Value>>,
}

/// `scheme://host[:port]` of a URL, lower-cased; `None` for non-http(s) URLs.
pub fn origin_of(url: &str) -> Option<String> {
    let (scheme, rest) = url.split_once("://")?;
    let scheme = scheme.to_ascii_lowercase();
    if scheme != "http" && scheme != "https" {
        return None;
    }
    let end = rest.find(|c| c == '/' || c == '?' || c == '#').unwrap_or(rest.len());
    let host = &rest[..end];
    if host.is_empty() {
        return None;
    }
    Some(format!("{}://{}", scheme, host.to_ascii_lowercase()))
}

fn clean_headers(headers: Option<&Value>) -> Value {
    let mut out = Map::new();
    if let Some(Value::Object(map)) = headers {
        for (k, v) in map {
            if CREDENTIAL_HEADERS.contains(&k.to_ascii_lowercase().as_str()) {
                continue;
            }
            let value = match v {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            out.insert(k.clone(), Value::String(value));
        }
    }
    Value::Object(out)
}

fn truncate(text: String) -> (String, bool) {
    if text.len() <= MAX_BODY_CHARS {
        return (text, false);
    }
    let mut cut = MAX_BODY_CHARS;
    while !text.is_char_boundary(cut) {
        cut -= 1;
    }
    (text[..cut].to_string(), true)
}

pub struct Recorder {
    origin: String,
    next_seq: u64,
    clock_offset: Option<f64>, // wallTime - monotonic timestamp
    pending: HashMap<String, Pending>,
    hops: HashMap<String, u32>,
    outbox: Vec<Value>,
    captured: u64,
    ignored: u64,
}

impl Recorder {
    pub fn new(origin: &str) -> Result<Recorder, String> {
        let normalized = origin_of(origin).ok_or_else(|| format!("invalid IFS origin {origin:?}"))?;
        Ok(Recorder {
            origin: normalized,
            next_seq: 1,
            clock_offset: None,
            pending: HashMap::new(),
            hops: HashMap::new(),
            outbox: Vec::new(),
            captured: 0,
            ignored: 0,
        })
    }

    pub fn origin(&self) -> &str {
        &self.origin
    }

    /// Wall-clock seconds for a CDP monotonic timestamp.
    fn wall(&self, timestamp: Option<f64>) -> f64 {
        match (timestamp, self.clock_offset) {
            (Some(t), Some(off)) => t + off,
            _ => now_seconds(),
        }
    }

    /// Feed one CDP event; returns the CDP work the caller must do next.
    pub fn on_event(&mut self, method: &str, params: &Value) -> Vec<Action> {
        let id = params.get("requestId").and_then(Value::as_str).unwrap_or("").to_string();
        let timestamp = params.get("timestamp").and_then(Value::as_f64);
        match method {
            "Network.requestWillBeSent" => self.request_will_be_sent(&id, params, timestamp),
            "Network.responseReceived" => {
                if let Some(p) = self.pending.get_mut(&id) {
                    let r = params.get("response").cloned().unwrap_or(Value::Null);
                    let mut meta = Map::new();
                    meta.insert("status".into(), r.get("status").cloned().unwrap_or(json!(0)));
                    meta.insert("status_text".into(), r.get("statusText").cloned().unwrap_or(json!("")));
                    meta.insert("headers".into(), clean_headers(r.get("headers")));
                    meta.insert("mime_type".into(), r.get("mimeType").cloned().unwrap_or(json!("")));
                    p.response = Some(meta);
                }
                vec![]
            }
            "Network.loadingFinished" => {
                if self.pending.contains_key(&id) {
                    let ts = self.wall(timestamp);
                    if let Some(meta) = self.pending.get_mut(&id).and_then(|p| p.response.as_mut()) {
                        meta.insert("ts".into(), json!(ts));
                    }
                    vec![Action::FetchBody(id)]
                } else {
                    vec![]
                }
            }
            "Network.loadingFailed" => {
                if self.pending.contains_key(&id) {
                    let ts = self.wall(timestamp);
                    self.emit_request(&id);
                    let p = self.pending.remove(&id).expect("pending");
                    let text = params.get("errorText").and_then(Value::as_str).unwrap_or("failed");
                    self.outbox.push(json!({"type": "failed", "seq": p.seq, "request_id": p.hop_id, "ts": ts,
                                            "error_text": text}));
                }
                vec![]
            }
            _ => vec![],
        }
    }

    fn request_will_be_sent(&mut self, id: &str, params: &Value, timestamp: Option<f64>) -> Vec<Action> {
        if let (Some(wall), Some(t)) = (params.get("wallTime").and_then(Value::as_f64), timestamp) {
            self.clock_offset = Some(wall - t);
        }
        let ts = params.get("wallTime").and_then(Value::as_f64).unwrap_or_else(|| self.wall(timestamp));
        if let Some(redirect) = params.get("redirectResponse") {
            // The previous hop ended with a redirect: close it, then track the new hop under a new id.
            if self.pending.contains_key(id) {
                self.emit_request(id);
                let p = self.pending.remove(id).expect("pending");
                self.outbox.push(json!({"type": "response", "seq": p.seq, "request_id": p.hop_id, "ts": ts,
                                        "status": redirect.get("status").cloned().unwrap_or(json!(0)),
                                        "status_text": redirect.get("statusText").cloned().unwrap_or(json!("")),
                                        "headers": clean_headers(redirect.get("headers")),
                                        "mime_type": "", "body": Value::Null, "base64": false}));
            }
        }
        let req = params.get("request").cloned().unwrap_or(Value::Null);
        let url = req.get("url").and_then(Value::as_str).unwrap_or("");
        let method = req.get("method").and_then(Value::as_str).unwrap_or("GET").to_ascii_uppercase();
        let kind = params.get("type").and_then(Value::as_str).unwrap_or("Other");
        if origin_of(url).as_deref() != Some(self.origin.as_str()) || !CAPTURED_TYPES.contains(&kind)
            || method == "OPTIONS"
        {
            self.ignored += 1;
            return vec![];
        }
        let hop = self.hops.entry(id.to_string()).or_insert(0);
        let hop_id = if *hop == 0 { id.to_string() } else { format!("{id}:{hop}") };
        *hop += 1;
        let seq = self.next_seq;
        self.next_seq += 1;
        self.captured += 1;
        let mut event = json!({"type": "request", "seq": seq, "request_id": hop_id, "ts": ts, "method": method,
                               "url": url, "headers": clean_headers(req.get("headers")), "resource_type": kind});
        let needs_post_data = req.get("postData").is_none()
            && req.get("hasPostData").and_then(Value::as_bool).unwrap_or(false);
        if let Some(Value::String(data)) = req.get("postData") {
            let (text, cut) = truncate(data.clone());
            event["post_data"] = json!(text);
            if cut {
                event["post_data_truncated"] = json!(true);
            }
        }
        let mut pending = Pending { hop_id, seq, request: Some(event), emitted: false, response: None };
        if needs_post_data {
            self.pending.insert(id.to_string(), pending);
            return vec![Action::FetchPostData(id.to_string())];
        }
        self.outbox.push(pending.request.take().expect("request"));
        pending.emitted = true;
        self.pending.insert(id.to_string(), pending);
        vec![]
    }

    fn emit_request(&mut self, id: &str) {
        if let Some(p) = self.pending.get_mut(id) {
            if !p.emitted {
                if let Some(event) = p.request.take() {
                    self.outbox.push(event);
                }
                p.emitted = true;
            }
        }
    }

    /// Result of `Network.getRequestPostData` (None if CDP could not return it).
    pub fn on_post_data(&mut self, id: &str, data: Option<String>) {
        if let Some(p) = self.pending.get_mut(id) {
            if let Some(event) = p.request.as_mut() {
                match data {
                    Some(d) => {
                        let (text, cut) = truncate(d);
                        event["post_data"] = json!(text);
                        if cut {
                            event["post_data_truncated"] = json!(true);
                        }
                    }
                    None => event["post_data_unavailable"] = json!(true),
                }
            }
        }
        self.emit_request(id);
    }

    /// Result of `Network.getResponseBody` (None if CDP could not return it).
    pub fn on_body(&mut self, id: &str, body: Option<String>, base64: bool) {
        if !self.pending.contains_key(id) {
            return;
        }
        self.emit_request(id);
        let p = self.pending.remove(id).expect("pending");
        let mut meta = p.response.unwrap_or_default();
        let ts = meta.remove("ts").unwrap_or_else(|| json!(now_seconds()));
        let mut event = json!({"type": "response", "seq": p.seq, "request_id": p.hop_id, "ts": ts,
                               "base64": base64 && body.is_some()});
        for (k, v) in meta {
            event[k.as_str()] = v;
        }
        match body {
            Some(b) => {
                let (text, cut) = truncate(b);
                event["body"] = json!(text);
                if cut {
                    event["body_truncated"] = json!(true);
                }
            }
            None => event["body"] = Value::Null,
        }
        self.outbox.push(event);
    }

    /// Emit requests still waiting for post data (before stopping: requests sent before the stop must
    /// reach the toolkit while it is still recording).
    pub fn flush_requests(&mut self) {
        let ids: Vec<String> = self.pending.iter().filter(|(_, p)| !p.emitted).map(|(k, _)| k.clone()).collect();
        let mut ordered: Vec<(u64, String)> = ids.into_iter().map(|k| (self.pending[&k].seq, k)).collect();
        ordered.sort();
        for (_, id) in ordered {
            if let Some(event) = self.pending.get_mut(&id).and_then(|p| p.request.as_mut()) {
                event["post_data_unavailable"] = json!(true);
            }
            self.emit_request(&id);
        }
    }

    /// Requests still waiting for a response body.
    pub fn pending(&self) -> usize {
        self.pending.len()
    }

    pub fn queued(&self) -> usize {
        self.outbox.len()
    }

    /// Up to `max_bytes` of queued events (at least one) as the toolkit's `{"events": [...]}` body,
    /// or `None` when nothing is queued. Events leave in the order they were queued.
    pub fn take_batch(&mut self, max_bytes: usize) -> Option<String> {
        if self.outbox.is_empty() {
            return None;
        }
        let mut size = 0;
        let mut count = 0;
        for e in &self.outbox {
            let n = e.to_string().len() + 1;
            if count > 0 && size + n > max_bytes {
                break;
            }
            size += n;
            count += 1;
        }
        let batch: Vec<Value> = self.outbox.drain(..count).collect();
        Some(json!({"events": batch}).to_string())
    }

    pub fn stats(&self) -> Value {
        json!({"origin": self.origin, "captured": self.captured, "ignored": self.ignored,
               "pending": self.pending.len(), "queued": self.outbox.len(), "protocol": PROTOCOL_VERSION})
    }
}

/// Body for a session command (`complete`, `skip`, `stop`): the click time, so the task boundary is
/// placed exactly between the requests sent before and after it.
pub fn command_payload(ts: f64, task_id: Option<&str>) -> String {
    match task_id {
        Some(t) => json!({"ts": ts, "task_id": t}).to_string(),
        None => json!({"ts": ts}).to_string(),
    }
}

#[cfg(not(target_arch = "wasm32"))]
fn now_seconds() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

#[cfg(all(target_arch = "wasm32", feature = "wasm"))]
fn now_seconds() -> f64 {
    wasm::date_now() / 1000.0
}

#[cfg(all(target_arch = "wasm32", not(feature = "wasm")))]
fn now_seconds() -> f64 {
    0.0
}

#[cfg(test)]
mod tests;
