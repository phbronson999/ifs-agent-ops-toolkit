use super::*;
use serde_json::{json, Value};

const ORIGIN: &str = "https://ifs.example";

fn sent(rec: &mut Recorder, id: &str, url: &str, kind: &str, wall: f64, extra: Value) -> Vec<Action> {
    let mut request = json!({"url": url, "method": "post", "headers": {
        "Authorization": "Bearer secret", "Cookie": "s=1", "Content-Type": "application/json", "If-Match": "W/\"1\""}});
    if let Value::Object(map) = extra {
        for (k, v) in map {
            request[k.as_str()] = v;
        }
    }
    rec.on_event("Network.requestWillBeSent", &json!({"requestId": id, "request": request, "type": kind,
                                                       "timestamp": wall - 1000.0, "wallTime": wall}))
}

fn events(rec: &mut Recorder) -> Vec<Value> {
    let body: Value = serde_json::from_str(&rec.take_batch(usize::MAX).unwrap_or("{\"events\":[]}".into())).unwrap();
    body["events"].as_array().cloned().unwrap_or_default()
}

#[test]
fn only_ifs_xhr_and_fetch_are_captured_and_credentials_never_leave() {
    let mut rec = Recorder::new("https://IFS.example/main/ifsapplications/web/").unwrap();
    assert_eq!(rec.origin(), ORIGIN);
    sent(&mut rec, "1", "https://ifs.example/main/ifsapplications/projection/v1/X.svc/A", "XHR", 100.0,
         json!({"postData": "{\"a\":1}"}));
    sent(&mut rec, "2", "https://ifs.example/main/app.js", "Script", 101.0, json!({}));
    sent(&mut rec, "3", "https://other.example/api", "Fetch", 102.0, json!({}));
    sent(&mut rec, "4", "https://ifs.example/x", "Fetch", 103.0, json!({"method": "OPTIONS"}));
    let out = events(&mut rec);
    assert_eq!(out.len(), 1);
    let e = &out[0];
    assert_eq!((e["type"].as_str(), e["seq"].as_u64(), e["method"].as_str()), (Some("request"), Some(1), Some("POST")));
    assert_eq!(e["ts"].as_f64(), Some(100.0));
    assert_eq!(e["post_data"].as_str(), Some("{\"a\":1}"));
    let headers = e["headers"].as_object().unwrap();
    assert!(headers.contains_key("If-Match") && headers.contains_key("Content-Type"));
    assert!(!headers.keys().any(|k| k.eq_ignore_ascii_case("authorization") || k.eq_ignore_ascii_case("cookie")));
    assert_eq!(rec.stats()["ignored"].as_u64(), Some(3));
}

#[test]
fn responses_follow_their_request_with_body_and_converted_time() {
    let mut rec = Recorder::new(ORIGIN).unwrap();
    sent(&mut rec, "7", "https://ifs.example/p", "Fetch", 200.0, json!({}));
    rec.on_event("Network.responseReceived", &json!({"requestId": "7", "timestamp": -799.5, "response": {
        "status": 201, "statusText": "Created", "mimeType": "application/json",
        "headers": {"Set-Cookie": "x", "ETag": "W/\"2\""}}}));
    let actions = rec.on_event("Network.loadingFinished", &json!({"requestId": "7", "timestamp": -799.0}));
    assert_eq!(actions, vec![Action::FetchBody("7".into())]);
    rec.on_body("7", Some("{\"OrderNo\":\"1\"}".into()), false);
    let out = events(&mut rec);
    assert_eq!(out.iter().map(|e| e["type"].as_str().unwrap()).collect::<Vec<_>>(), ["request", "response"]);
    let r = &out[1];
    assert_eq!((r["status"].as_i64(), r["seq"].as_u64(), r["ts"].as_f64()), (Some(201), Some(1), Some(201.0)));
    assert!(r["headers"].get("ETag").is_some() && r["headers"].get("Set-Cookie").is_none());
    assert_eq!(rec.pending(), 0);
}

#[test]
fn post_data_fetched_later_still_precedes_the_response() {
    let mut rec = Recorder::new(ORIGIN).unwrap();
    let actions = sent(&mut rec, "9", "https://ifs.example/big", "XHR", 300.0, json!({"hasPostData": true}));
    assert_eq!(actions, vec![Action::FetchPostData("9".into())]);
    sent(&mut rec, "10", "https://ifs.example/small", "XHR", 301.0, json!({}));
    rec.on_event("Network.loadingFinished", &json!({"requestId": "9", "timestamp": -698.0}));
    rec.on_body("9", Some("ok".into()), false); // body arrived before post data: request is emitted first
    let out = events(&mut rec);
    let order: Vec<(String, u64)> = out.iter()
        .map(|e| (e["type"].as_str().unwrap().to_string(), e["seq"].as_u64().unwrap())).collect();
    assert_eq!(order, [("request".into(), 2), ("request".into(), 1), ("response".into(), 1)]);
    assert_eq!(out[1]["post_data_unavailable"].as_bool(), None);
}

#[test]
fn failures_redirects_and_flush() {
    let mut rec = Recorder::new(ORIGIN).unwrap();
    sent(&mut rec, "1", "https://ifs.example/a", "XHR", 10.0, json!({}));
    rec.on_event("Network.loadingFailed", &json!({"requestId": "1", "timestamp": -989.0, "errorText": "net::ERR"}));
    sent(&mut rec, "2", "https://ifs.example/r", "XHR", 11.0, json!({}));
    rec.on_event("Network.requestWillBeSent", &json!({"requestId": "2", "type": "XHR", "timestamp": -988.0,
        "wallTime": 12.0, "redirectResponse": {"status": 302, "headers": {}},
        "request": {"url": "https://ifs.example/r2", "method": "GET", "headers": {}}}));
    sent(&mut rec, "3", "https://ifs.example/p", "XHR", 13.0, json!({"hasPostData": true}));
    rec.flush_requests();
    let out = events(&mut rec);
    let ids: Vec<(&str, &str)> = out.iter().map(|e| (e["type"].as_str().unwrap(), e["request_id"].as_str().unwrap()))
        .collect();
    assert_eq!(ids, [("request", "1"), ("failed", "1"), ("request", "2"), ("response", "2"), ("request", "2:1"),
                     ("request", "3")]);
    assert_eq!(out[3]["status"].as_i64(), Some(302));
    assert_eq!(out[5]["post_data_unavailable"].as_bool(), Some(true));
}

#[test]
fn batches_respect_the_size_limit_and_keep_order() {
    let mut rec = Recorder::new(ORIGIN).unwrap();
    for i in 0..5 {
        sent(&mut rec, &i.to_string(), "https://ifs.example/x", "XHR", 1.0 + i as f64, json!({}));
    }
    let first: Value = serde_json::from_str(&rec.take_batch(600).unwrap()).unwrap();
    let n = first["events"].as_array().unwrap().len();
    assert!((1..5).contains(&n));
    let rest: Value = serde_json::from_str(&rec.take_batch(usize::MAX).unwrap()).unwrap();
    assert_eq!(rest["events"][0]["seq"].as_u64(), Some(n as u64 + 1));
    assert!(rec.take_batch(100).is_none());
    assert_eq!(command_payload(5.5, Some("t2")), "{\"task_id\":\"t2\",\"ts\":5.5}");
    assert_eq!(origin_of("ftp://x"), None);
}
