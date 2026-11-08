//! The kx panel for NextCode. Every invocation runs `kx nextcode panel` in the project, and what it prints is the
//! response (`"response": "stdout"`: NextCode validates it and does not come back here): kx draws the panel
//! (src/kx/views/nextcode.py), this module only starts it and shows its failures. It implements NextCode's
//! language-neutral extension ABI (API 3) without the SDK, so it builds from the kx repo alone.

use serde::Deserialize;
use serde_json::value::RawValue;
use serde_json::{Value, json};

const REFRESH_MS: u64 = 10_000;

/// What this module reads of a request; the rest (and any field added later) is skipped unread. A process-exit
/// request can carry 256 KiB of each output stream, so it is read in one pass; the others are small and read twice
/// (here and as the raw invocation passed on to kx).
#[derive(Deserialize)]
struct Request<'a> {
    #[serde(borrow)]
    invocation: Invocation<'a>,
}

#[derive(Deserialize)]
struct Invocation<'a> {
    kind: &'a str,
    #[serde(default)]
    panel: &'a str,
    #[serde(default)]
    process: Option<Process>,
}

#[derive(Deserialize)]
struct Process {
    exit_code: Option<i64>,
    #[serde(default)]
    stderr: String,
    error: Option<String>,
}

#[derive(Deserialize)]
struct Raw<'a> {
    #[serde(borrow)]
    invocation: &'a RawValue,
}

/// One invocation: the request's JSON in, the response's JSON out.
pub fn handle(request: &[u8]) -> Vec<u8> {
    let invocation = match serde_json::from_slice::<Request>(request) {
        Ok(r) => r.invocation,
        Err(e) => return error(&format!("invalid request: {e}")),
    };
    let raw = || serde_json::from_slice::<Raw>(request).expect("read once already").invocation;
    match (invocation.kind, invocation.process) {
        ("process-exit", Some(process)) => exited(invocation.panel, process),
        // no panel: opening shows NextCode's placeholder, an event or a refresh keeps what is shown until kx answers
        ("open-panel" | "ui-event" | "refresh", _) => to_vec(json!({"processes": [kx(raw())]})),
        (kind, _) => error(&format!("unexpected invocation {kind:?}")),
    }
}

/// `kx nextcode panel`, told what happened (the event, the panel's inputs and state) in the invocation's own JSON.
fn kx(invocation: &RawValue) -> Value {
    json!({"id": "panel", "program": "kx", "timeout_ms": 30000, "response": "stdout",
           "args": ["nextcode", "panel", "--invocation", invocation.get()]})
}

/// kx failed (a successful run never comes back here): say so in the panel, and retry at the next refresh.
fn exited(panel: &str, process: Process) -> Vec<u8> {
    let why = match (process.exit_code, &process.error) {
        (_, Some(e)) => format!("kx could not run: {e}"),
        (Some(0), None) => "kx's output was cut short".to_owned(), // over 256 KiB: complete output never comes here
        (Some(code), None) => format!("kx exited with code {code}"),
        (None, None) => "kx did not finish".to_owned(),
    };
    let stderr = tail(&process.stderr, 4000);
    let mut text = format!("**{why}**");
    if process.error.as_deref().is_some_and(|e| e.contains("not found") || e.contains("No such file")) {
        text.push_str("\n\nInstall kx where the project runs: `uv tool install --editable kx` (see kx's README).");
    }
    if !stderr.is_empty() {
        text.push_str(&format!("\n\n```\n{}\n```", stderr.replace("```", "'''")));
    }
    to_vec(json!({
        "panels": [{"id": panel, "state": {}, "refresh_ms": REFRESH_MS, "root": {
            "type": "column", "id": "root", "gap": 8, "children": [
                {"type": "row", "id": "head", "gap": 8, "children": [
                    {"type": "markdown", "id": "title", "text": "# kx"},
                    {"type": "button", "id": "refresh", "text": "Refresh", "disabled": false}]},
                {"type": "markdown", "id": "error", "text": text}]}}],
        "logs": [format!("{why}{}", if stderr.is_empty() { String::new() } else { format!(": {stderr}") })],
    }))
}

fn error(message: &str) -> Vec<u8> {
    to_vec(json!({"error": message}))
}

fn to_vec(v: Value) -> Vec<u8> {
    serde_json::to_vec(&v).expect("a JSON value serializes")
}

/// The last `n` bytes of `s`, from a character boundary.
fn tail(s: &str, n: usize) -> &str {
    let s = s.trim_end();
    let mut start = s.len().saturating_sub(n);
    while !s.is_char_boundary(start) {
        start += 1;
    }
    &s[start..]
}

/// The ABI's two exports. The host writes the request into the buffer `nextcode_alloc` returns, and copies the
/// response out before it drops the instance (one instance per invocation), so both buffers live in statics.
#[cfg(target_arch = "wasm32")]
mod abi {
    use std::sync::Mutex;

    static INPUT: Mutex<Vec<u8>> = Mutex::new(Vec::new());
    static OUTPUT: Mutex<Vec<u8>> = Mutex::new(Vec::new());

    #[unsafe(no_mangle)]
    pub extern "C" fn nextcode_alloc(len: i32) -> i32 {
        let mut input = INPUT.lock().unwrap_or_else(|p| p.into_inner());
        *input = vec![0; len.max(0) as usize];
        input.as_mut_ptr() as i32
    }

    #[unsafe(no_mangle)]
    pub extern "C" fn nextcode_run(ptr: i32, len: i32) -> i64 {
        let input = INPUT.lock().unwrap_or_else(|p| p.into_inner());
        let response = if ptr == input.as_ptr() as i32 && len as usize == input.len() {
            super::handle(&input)
        } else {
            super::error("nextcode_run got a buffer nextcode_alloc did not return")
        };
        let mut output = OUTPUT.lock().unwrap_or_else(|p| p.into_inner());
        *output = response;
        ((output.len() as i64) << 32) | (output.as_ptr() as u32 as i64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call(invocation: Value) -> Value {
        let request = json!({"api_version": 3, "invocation": invocation, "document": null, "settings": {}});
        serde_json::from_slice(&handle(request.to_string().as_bytes())).unwrap()
    }

    #[test]
    fn every_invocation_runs_kx_with_it_and_kx_answers_directly() {
        for invocation in [json!({"kind": "open-panel", "id": "overview"}),
                           json!({"kind": "refresh", "panel": "overview", "revision": 1, "state": {}, "inputs": {}}),
                           json!({"kind": "ui-event", "panel": "overview", "revision": 1, "state": {"protocol": "cv5"},
                                  "inputs": {"protocol": "cv5"}, "event": {"widget": "refresh", "kind": {"kind": "click"}}})] {
            let r = call(invocation.clone());
            assert!(r.get("panels").is_none(), "{invocation}");
            let p = &r["processes"][0];
            assert_eq!((&p["program"], &p["response"]), (&json!("kx"), &json!("stdout")));
            assert_eq!(p["args"].as_array().unwrap()[..3], [json!("nextcode"), json!("panel"), json!("--invocation")]);
            assert_eq!(serde_json::from_str::<Value>(p["args"][3].as_str().unwrap()).unwrap(), invocation);
        }
    }

    #[test]
    fn a_failure_is_shown_in_the_panel() {
        let r = call(json!({"kind": "process-exit", "panel": "overview", "revision": 2, "state": {}, "inputs": {},
                            "process": {"id": "panel", "exit_code": 0, "stdout": "{\"panels\":[", "stderr": "",
                                        "truncated": true, "error": null}}));
        assert!(r["panels"][0]["root"]["children"][1]["text"].as_str().unwrap().contains("cut short"));
        let r = call(json!({"kind": "process-exit", "panel": "overview", "revision": 2, "state": {}, "inputs": {},
                            "process": {"id": "panel", "exit_code": null, "stdout": "", "stderr": "",
                                        "truncated": false, "error": "kx: not found on PATH"}}));
        let text = r["panels"][0]["root"]["children"][1]["text"].as_str().unwrap();
        assert!(text.contains("not found") && text.contains("uv tool install"), "{text}");
        let r = call(json!({"kind": "process-exit", "panel": "training", "revision": 2, "state": {}, "inputs": {},
                            "process": {"id": "panel", "exit_code": 1, "stdout": "", "stderr": "Traceback ...\nBoom",
                                        "truncated": false, "error": null}}));
        assert!(r["panels"][0]["root"]["children"][1]["text"].as_str().unwrap().contains("Boom"));
        assert_eq!(r["panels"][0]["refresh_ms"], REFRESH_MS);
        assert_eq!(r["panels"][0]["id"], "training");  // the panel that ran kx, whichever it is
    }
}
