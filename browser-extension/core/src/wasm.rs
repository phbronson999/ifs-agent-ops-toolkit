//! wasm-bindgen surface used by the extension's side panel (`extension/sidepanel.js`).

use crate::{command_payload, origin_of, Recorder, PROTOCOL_VERSION};
use serde_json::Value;
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
extern "C" {
    #[wasm_bindgen(js_namespace = Date, js_name = now)]
    pub fn date_now() -> f64;
}

#[wasm_bindgen]
pub struct CaptureCore {
    inner: Recorder,
}

#[wasm_bindgen]
impl CaptureCore {
    #[wasm_bindgen(constructor)]
    pub fn new(ifs_origin: &str) -> Result<CaptureCore, JsError> {
        Recorder::new(ifs_origin).map(|inner| CaptureCore { inner }).map_err(|e| JsError::new(&e))
    }

    /// Feed a CDP event (`params` as JSON text); returns a JSON array of actions for JS to perform.
    pub fn on_event(&mut self, method: &str, params_json: &str) -> Result<String, JsError> {
        let params: Value = serde_json::from_str(params_json).map_err(|e| JsError::new(&e.to_string()))?;
        let actions: Vec<Value> = self.inner.on_event(method, &params).iter().map(|a| a.to_json()).collect();
        Ok(Value::Array(actions).to_string())
    }

    pub fn on_post_data(&mut self, request_id: &str, data: Option<String>) {
        self.inner.on_post_data(request_id, data);
    }

    pub fn on_body(&mut self, request_id: &str, body: Option<String>, base64: bool) {
        self.inner.on_body(request_id, body, base64);
    }

    pub fn flush_requests(&mut self) {
        self.inner.flush_requests();
    }

    /// Next `{"events": [...]}` request body for the toolkit, or undefined when nothing is queued.
    pub fn take_batch(&mut self, max_bytes: usize) -> Option<String> {
        self.inner.take_batch(max_bytes)
    }

    pub fn pending(&self) -> usize {
        self.inner.pending()
    }

    pub fn queued(&self) -> usize {
        self.inner.queued()
    }

    pub fn origin(&self) -> String {
        self.inner.origin().to_string()
    }

    pub fn stats(&self) -> String {
        self.inner.stats().to_string()
    }
}

#[wasm_bindgen(js_name = originOf)]
pub fn origin_of_js(url: &str) -> Option<String> {
    origin_of(url)
}

#[wasm_bindgen(js_name = commandPayload)]
pub fn command_payload_js(ts: f64, task_id: Option<String>) -> String {
    command_payload(ts, task_id.as_deref())
}

#[wasm_bindgen(js_name = protocolVersion)]
pub fn protocol_version() -> u32 {
    PROTOCOL_VERSION
}
