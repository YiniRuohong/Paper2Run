//! Desktop side: accept allow-listed peers and reverse-proxy each QUIC stream
//! as one HTTP/1.1 connection to the loopback backend.

use std::{
    collections::HashMap,
    convert::Infallible,
    path::PathBuf,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};

use bytes::Bytes;
use http::{header, HeaderValue, Request, Response, StatusCode, Uri};
use http_body_util::{combinators::BoxBody, BodyExt, Full};
use hyper::body::Incoming;
use hyper_util::{
    client::legacy::{connect::HttpConnector, Client},
    rt::{TokioExecutor, TokioIo},
};
use iroh::{endpoint::Connection, Endpoint, EndpointId};

use crate::paths::PathKind;

type Body = BoxBody<Bytes, hyper::Error>;
type Upstream = Client<HttpConnector, Incoming>;

/// Close code sent to a peer that is not (or no longer) paired.
pub const CLOSE_REVOKED: u32 = 2;

/// Live connections per peer, for the control API's status and revoke.
#[derive(Clone, Default)]
pub struct Registry(Arc<tokio::sync::Mutex<HashMap<EndpointId, Entry>>>);

struct Entry {
    conn: Option<(u64, Connection)>,
    last_seen: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LiveInfo {
    pub online: bool,
    pub path: Option<PathKind>,
    pub last_seen: u64,
}

static GENERATION: AtomicU64 = AtomicU64::new(0);

impl Registry {
    pub async fn snapshot(&self) -> HashMap<EndpointId, LiveInfo> {
        self.0.lock().await.iter().map(|(id, e)| {
            let live = e.conn.as_ref().filter(|(_, c)| c.close_reason().is_none());
            let path = live.map(|(_, c)| crate::paths::selected(c).0).filter(|k| *k != PathKind::Unknown);
            (*id, LiveInfo { online: live.is_some(), path, last_seen: e.last_seen })
        }).collect()
    }

    /// Close the peer's live connection, telling it it was revoked.
    pub async fn revoke(&self, id: EndpointId) {
        if let Some(e) = self.0.lock().await.remove(&id) {
            if let Some((_, c)) = e.conn {
                c.close(CLOSE_REVOKED.into(), b"revoked");
            }
        }
    }

    async fn attach(&self, conn: Connection) {
        let generation = GENERATION.fetch_add(1, Ordering::Relaxed);
        let id = conn.remote_id();
        self.0.lock().await.insert(id, Entry { conn: Some((generation, conn.clone())), last_seen: crate::devices::now_unix() });
        let reg = self.clone();
        tokio::spawn(async move {
            conn.closed().await;
            if let Some(e) = reg.0.lock().await.get_mut(&id) {
                if e.conn.as_ref().is_some_and(|(g, _)| *g == generation) {
                    e.conn = None;
                    e.last_seen = crate::devices::now_unix();
                }
            }
        });
    }

    async fn touch(&self, id: EndpointId) {
        if let Some(e) = self.0.lock().await.get_mut(&id) {
            e.last_seen = crate::devices::now_unix();
        }
    }
}

pub async fn run(ep: Endpoint, backend_port: u16, allow_path: PathBuf) -> anyhow::Result<()> {
    run_with(ep, backend_port, allow_path, None).await
}

/// Like [`run`], also handing ALPN_PAIR connections to `pairing` when given.
pub async fn run_with(ep: Endpoint, backend_port: u16, allow_path: PathBuf, pairing: Option<crate::pair::Pairing>) -> anyhow::Result<()> {
    run_full(ep, backend_port, allow_path, pairing, Registry::default()).await
}

/// Like [`run_with`], tracking live peers in `registry`.
pub async fn run_full(ep: Endpoint, backend_port: u16, allow_path: PathBuf, pairing: Option<crate::pair::Pairing>, registry: Registry) -> anyhow::Result<()> {
    let client: Upstream = Client::builder(TokioExecutor::new()).build_http();
    let ctx = Arc::new(Ctx { client, backend_port, allow_path: allow_path.clone(), registry });
    while let Some(incoming) = ep.accept().await {
        let ctx = ctx.clone();
        let allow_path = allow_path.clone();
        let pairing = pairing.clone();
        tokio::spawn(async move {
            let conn = match incoming.await {
                Ok(c) => c,
                Err(e) => return tracing::debug!("handshake failed: {e}"),
            };
            if conn.alpn() == crate::endpoint::ALPN_PAIR {
                if let Some(p) = pairing { p.handle(conn).await } else { conn.close(1u32.into(), b"pairing closed") }
                return;
            }
            if conn.alpn() != crate::endpoint::ALPN {
                return conn.close(1u32.into(), b"unsupported protocol");
            }
            let peer = conn.remote_id();
            match crate::allow::load(&allow_path) {
                Ok(set) if set.contains(&peer) => {}
                Ok(_) => {
                    tracing::warn!("peer={peer} rejected: not in allow-list");
                    return conn.close(CLOSE_REVOKED.into(), b"revoked");
                }
                Err(e) => {
                    tracing::error!("allow-list unreadable, rejecting peer={peer}: {e:#}");
                    return conn.close(1u32.into(), b"allow-list unreadable");
                }
            }
            crate::paths::spawn_logger(conn.clone());
            ctx.registry.attach(conn.clone()).await;
            serve_conn(conn, ctx).await;
        });
    }
    Ok(())
}

struct Ctx {
    client: Upstream,
    backend_port: u16,
    allow_path: PathBuf,
    registry: Registry,
}

fn still_allowed(ctx: &Ctx, peer: iroh::EndpointId) -> bool {
    matches!(crate::allow::load(&ctx.allow_path), Ok(set) if set.contains(&peer))
}

async fn serve_conn(conn: Connection, ctx: Arc<Ctx>) {
    let peer = conn.remote_id();
    while let Ok((send, recv)) = conn.accept_bi().await {
        // Re-check on every stream so removing a peer cuts a live connection.
        if !still_allowed(&ctx, peer) {
            tracing::warn!("peer={peer} revoked; closing connection");
            conn.close(CLOSE_REVOKED.into(), b"revoked");
            return;
        }
        ctx.registry.touch(peer).await;
        let ctx = ctx.clone();
        tokio::spawn(async move {
            let io = TokioIo::new(tokio::io::join(recv, send));
            let svc = hyper::service::service_fn(move |req| proxy(req, ctx.clone()));
            if let Err(e) = hyper::server::conn::http1::Builder::new().serve_connection(io, svc).await {
                tracing::debug!("stream ended: {e}");
            }
        });
    }
}

/// The tablet's browser must be talking to its own loopback proxy, same-origin.
/// Anything else (a rebound hostname, a foreign Origin, a cross-site fetch) is a
/// web page on the tablet trying to drive the desktop, so refuse it before the
/// rewrite below would launder it into trusted loopback values.
fn browser_context_ok(headers: &http::HeaderMap) -> bool {
    let is_loopback = |host: &str| {
        let name = host.rsplit_once(':').map_or(host, |(h, _)| h);
        name.eq_ignore_ascii_case("127.0.0.1") || name.eq_ignore_ascii_case("localhost")
    };
    let host_ok = headers.get(header::HOST).and_then(|v| v.to_str().ok()).is_some_and(is_loopback);
    let origin_ok = match headers.get(header::ORIGIN).map(|v| v.to_str()) {
        None => true,
        Some(Ok(o)) => o.strip_prefix("http://").is_some_and(|rest| !rest.contains('/') && is_loopback(rest)),
        Some(Err(_)) => false,
    };
    let site_ok = !matches!(
        headers.get("sec-fetch-site").and_then(|v| v.to_str().ok()),
        Some(s) if s.eq_ignore_ascii_case("cross-site") || s.eq_ignore_ascii_case("same-site")
    );
    host_ok && origin_ok && site_ok
}

async fn proxy(mut req: Request<Incoming>, ctx: Arc<Ctx>) -> Result<Response<Body>, Infallible> {
    if !browser_context_ok(req.headers()) {
        tracing::warn!("refused request with non-loopback Host/Origin or cross-site fetch");
        return Ok(status(StatusCode::MISDIRECTED_REQUEST));
    }
    let authority = format!("127.0.0.1:{}", ctx.backend_port);
    let path = req.uri().path_and_query().map(|p| p.as_str()).unwrap_or("/");
    *req.uri_mut() = match format!("http://{authority}{path}").parse::<Uri>() {
        Ok(u) => u,
        Err(_) => return Ok(status(StatusCode::BAD_REQUEST)),
    };
    let headers = req.headers_mut();
    headers.insert(header::HOST, HeaderValue::from_str(&authority).expect("ascii"));
    // Only this proxy may say a request came from a paired device.
    headers.insert("x-p2r-remote", HeaderValue::from_static("1"));
    if headers.contains_key(header::ORIGIN) {
        headers.insert(header::ORIGIN, HeaderValue::from_str(&format!("http://{authority}")).expect("ascii"));
    }
    match ctx.client.request(req).await {
        Ok(resp) => Ok(resp.map(|b| b.boxed())),
        Err(e) => {
            tracing::warn!("backend unreachable: {e}");
            Ok(status(StatusCode::BAD_GATEWAY))
        }
    }
}

fn status(code: StatusCode) -> Response<Body> {
    let mut r = Response::new(Full::new(Bytes::from(code.canonical_reason().unwrap_or(""))).map_err(|e| match e {}).boxed());
    *r.status_mut() = code;
    r
}
