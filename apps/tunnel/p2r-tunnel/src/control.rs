//! Loopback-only control API for the desktop app (FastAPI proxies to it).
//! Every request needs `Authorization: Bearer <token>`.
//!
//!   GET    /status         {"desktop_id","relay","devices":[{id,name,paired_at,online,path,last_seen}]}
//!   POST   /pairing        {"code","expires_in_ms"}       (voids any previous code)
//!   GET    /pairing        {"state":"none|waiting|paired|expired|wrong_code", ...}
//!   DELETE /devices/{id}   204 | 404                       (also cuts its live connection)

use std::{
    convert::Infallible,
    path::{Path, PathBuf},
    sync::Arc,
};

use bytes::Bytes;
use http::{Method, Request, Response, StatusCode};
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper_util::rt::TokioIo;
use iroh::{Endpoint, EndpointId};
use serde_json::{json, Value};

use crate::{
    devices,
    endpoint::NetConfig,
    pair::{PairState, Pairing},
    serve::Registry,
};

pub struct ControlState {
    pub ep: Endpoint,
    pub cfg: NetConfig,
    pub rendezvous: EndpointId,
    pub pairing: Pairing,
    pub devices: PathBuf,
    pub registry: Registry,
    pub token: String,
}

pub fn new_token() -> String {
    use rand::RngCore;
    let mut b = [0u8; 32];
    rand::rng().fill_bytes(&mut b);
    b.iter().map(|x| format!("{x:02x}")).collect()
}

type Body = http_body_util::combinators::BoxBody<Bytes, Infallible>;

/// Bind 127.0.0.1:0 and serve in the background; returns the port.
pub async fn spawn(state: ControlState) -> anyhow::Result<u16> {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let port = listener.local_addr()?.port();
    let state = Arc::new(state);
    tokio::spawn(async move {
        while let Ok((sock, _)) = listener.accept().await {
            let state = state.clone();
            tokio::spawn(async move {
                let svc = hyper::service::service_fn(move |req| handle(req, state.clone()));
                let _ = hyper::server::conn::http1::Builder::new().serve_connection(TokioIo::new(sock), svc).await;
            });
        }
    });
    Ok(port)
}

fn reply(code: StatusCode, body: Value) -> Response<Body> {
    let mut r = Response::new(Full::new(Bytes::from(body.to_string())).boxed());
    *r.status_mut() = code;
    r.headers_mut().insert(http::header::CONTENT_TYPE, http::HeaderValue::from_static("application/json"));
    r
}

fn authorized(req: &Request<Incoming>, token: &str) -> bool {
    let Some(got) = req.headers().get(http::header::AUTHORIZATION).and_then(|v| v.to_str().ok()).and_then(|v| v.strip_prefix("Bearer ")) else {
        return false;
    };
    // Constant-time compare.
    got.len() == token.len() && got.bytes().zip(token.bytes()).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0
}

async fn handle(req: Request<Incoming>, st: Arc<ControlState>) -> Result<Response<Body>, Infallible> {
    if !authorized(&req, &st.token) {
        return Ok(reply(StatusCode::UNAUTHORIZED, json!({"error": "unauthorized"})));
    }
    let path = req.uri().path().to_string();
    let resp = match (req.method().clone(), path.as_str()) {
        (Method::GET, "/status") => status(&st).await,
        (Method::POST, "/pairing") => match st.pairing.start(&st.ep, &st.cfg, st.rendezvous).await {
            Ok(code) => {
                let ms = match st.pairing.state().await {
                    PairState::Waiting { expires_in, .. } => expires_in.as_millis() as u64,
                    _ => 0,
                };
                reply(StatusCode::OK, json!({"code": code.to_string(), "expires_in_ms": ms}))
            }
            Err(e) => reply(StatusCode::BAD_GATEWAY, json!({"error": format!("{e:#}")})),
        },
        (Method::GET, "/pairing") => reply(StatusCode::OK, pairing_json(st.pairing.state().await)),
        (Method::DELETE, p) if p.starts_with("/devices/") => match p["/devices/".len()..].parse::<EndpointId>() {
            Ok(id) => match devices::remove(&st.devices, id) {
                Ok(true) => {
                    st.registry.revoke(id).await;
                    let mut r = Response::new(Full::new(Bytes::new()).boxed());
                    *r.status_mut() = StatusCode::NO_CONTENT;
                    r
                }
                Ok(false) => reply(StatusCode::NOT_FOUND, json!({"error": "no such device"})),
                Err(e) => reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error": format!("{e:#}")})),
            },
            Err(_) => reply(StatusCode::BAD_REQUEST, json!({"error": "bad device id"})),
        },
        _ => reply(StatusCode::NOT_FOUND, json!({"error": "not found"})),
    };
    Ok(resp)
}

async fn status(st: &ControlState) -> Response<Body> {
    let list = match devices::list(&st.devices) {
        Ok(l) => l,
        Err(e) => return reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error": format!("{e:#}")})),
    };
    let live = st.registry.snapshot().await;
    let devices: Vec<Value> = list.iter().map(|d| {
        let l = live.get(&d.id);
        json!({
            "id": d.id.to_string(),
            "name": d.name,
            "paired_at": d.paired_at,
            "online": l.is_some_and(|l| l.online),
            "path": l.and_then(|l| l.path).map(|p| p.to_string()),
            "last_seen": l.map(|l| l.last_seen),
        })
    }).collect();
    reply(StatusCode::OK, json!({"desktop_id": st.ep.id().to_string(), "relay": st.cfg.relay_url.to_string(), "devices": devices}))
}

fn device_json(d: &devices::Device) -> Value {
    json!({"id": d.id.to_string(), "name": d.name, "paired_at": d.paired_at})
}

fn pairing_json(state: PairState) -> Value {
    match state {
        PairState::None => json!({"state": "none"}),
        PairState::Waiting { code, expires_in } => json!({"state": "waiting", "code": code.to_string(), "expires_in_ms": expires_in.as_millis() as u64}),
        PairState::Paired { device } => json!({"state": "paired", "device": device_json(&device)}),
        PairState::Expired => json!({"state": "expired"}),
        PairState::WrongCode => json!({"state": "wrong_code"}),
    }
}

/// `{"port","token","pid"}` at mode 0600; removed when dropped.
pub struct ControlFile {
    path: PathBuf,
}

impl ControlFile {
    pub fn write(path: &Path, port: u16, token: &str) -> anyhow::Result<Self> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let body = json!({"port": port, "token": token, "pid": std::process::id()});
        let tmp = path.with_extension("json.tmp");
        crate::keys::write_private(&tmp, body.to_string().as_bytes())?;
        std::fs::rename(&tmp, path)?;
        Ok(ControlFile { path: path.to_path_buf() })
    }
}

impl Drop for ControlFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}
