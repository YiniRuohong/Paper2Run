#![allow(dead_code)]
use iroh::{Endpoint, SecretKey};
use p2r_tunnel::endpoint::{bind_with, CaTlsConfig, NetConfig};

pub struct TestNet {
    pub cfg: NetConfig,
    _server: iroh_relay::server::Server,
}

pub async fn test_net(relay_only: bool) -> TestNet {
    let (_map, url, server) = iroh::test_utils::run_relay_server().await.unwrap();
    let quic = server.quic_addr().map(|a| a.port());
    TestNet { cfg: NetConfig { relay_url: url, relay_quic_port: quic, relay_ca_pem: None, relay_only }, _server: server }
}

pub async fn endpoint(net: &TestNet) -> Endpoint {
    bind_with(&net.cfg, SecretKey::generate(), CaTlsConfig::insecure_skip_verify()).await.unwrap()
}

pub async fn endpoint_with(net: &TestNet, key: SecretKey) -> Endpoint {
    bind_with(&net.cfg, key, CaTlsConfig::insecure_skip_verify()).await.unwrap()
}

// ---------- fake backend ----------
use std::{
    convert::Infallible,
    net::SocketAddr,
    sync::{atomic::{AtomicUsize, Ordering}, Arc},
    time::Duration,
};

use bytes::Bytes;
use http_body_util::{combinators::BoxBody, BodyExt, Full, StreamBody};
use hyper::{body::{Frame, Incoming}, Request, Response};
use hyper_util::rt::TokioIo;
use sha2::{Digest, Sha256};

pub struct Backend {
    pub port: u16,
    pub hits: Arc<AtomicUsize>,
}

type Body = BoxBody<Bytes, Infallible>;

fn full(s: impl Into<Bytes>) -> Body {
    Full::new(s.into()).boxed()
}

async fn handle(req: Request<Incoming>, hits: Arc<AtomicUsize>) -> Result<Response<Body>, Infallible> {
    hits.fetch_add(1, Ordering::SeqCst);
    let h = |n: &str| req.headers().get(n).map(|v| v.to_str().unwrap().to_string()).unwrap_or_else(|| "-".into());
    match req.uri().path() {
        "/healthz" => Ok(Response::new(full("ok"))),
        "/echo-headers" => {
            let text = format!("host={}\norigin={}\nsec-fetch-site={}\nx-p2r-remote={}\n", h("host"), h("origin"), h("sec-fetch-site"), h("x-p2r-remote"));
            Ok(Response::new(full(text)))
        }
        "/sse" => {
            let stream = n0_future::stream::unfold(0u32, |i| async move {
                if i >= 5 { return None; }
                tokio::time::sleep(Duration::from_millis(200)).await;
                Some((Ok::<_, Infallible>(Frame::data(Bytes::from(format!("data: {i}\n\n")))), i + 1))
            });
            let mut resp = Response::new(StreamBody::new(stream).boxed());
            resp.headers_mut().insert("content-type", "text/event-stream".parse().unwrap());
            Ok(resp)
        }
        "/hang-sse" => {
            let stream = n0_future::stream::unfold(0u32, |i| async move {
                tokio::time::sleep(Duration::from_secs(if i == 0 { 0 } else { 3600 })).await;
                Some((Ok::<_, Infallible>(Frame::data(Bytes::from_static(b"data: hi\n\n"))), i + 1))
            });
            Ok(Response::new(StreamBody::new(stream).boxed()))
        }
        "/upload" => {
            let mut body = req.into_body();
            let mut hasher = Sha256::new();
            while let Some(frame) = body.frame().await {
                if let Ok(data) = frame.unwrap().into_data() { hasher.update(&data); }
            }
            Ok(Response::new(full(hex(&hasher.finalize()))))
        }
        _ => Ok(Response::builder().status(404).body(full("nope")).unwrap()),
    }
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

pub async fn backend() -> Backend {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let hits = Arc::new(AtomicUsize::new(0));
    let h = hits.clone();
    tokio::spawn(async move {
        loop {
            let (sock, _) = listener.accept().await.unwrap();
            let h = h.clone();
            tokio::spawn(async move {
                let svc = hyper::service::service_fn(move |req| handle(req, h.clone()));
                let _ = hyper::server::conn::http1::Builder::new().serve_connection(TokioIo::new(sock), svc).await;
            });
        }
    });
    Backend { port, hits }
}

pub fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

pub fn write_allow(path: &std::path::Path, ids: &[iroh::EndpointId]) {
    let devices: Vec<_> = ids.iter().enumerate()
        .map(|(n, id)| serde_json::json!({"id": id.to_string(), "name": format!("dev{n}"), "paired_at": 0}))
        .collect();
    std::fs::write(path, serde_json::to_vec(&devices).unwrap()).unwrap();
}

/// Open one HTTP/1.1 connection over a fresh QUIC bi-stream.
pub async fn http_over_stream(
    conn: &iroh::endpoint::Connection,
) -> anyhow::Result<hyper::client::conn::http1::SendRequest<Body>> {
    let (send, recv) = conn.open_bi().await?;
    let io = TokioIo::new(tokio::io::join(recv, send));
    let (sender, driver) = hyper::client::conn::http1::handshake(io).await?;
    tokio::spawn(driver);
    Ok(sender)
}

pub fn get(path: &str, headers: &[(&str, &str)]) -> Request<Body> {
    let mut b = Request::get(path);
    for (k, v) in headers { b = b.header(*k, *v); }
    b.body(full("")).unwrap()
}

pub fn socket(port: u16) -> SocketAddr { SocketAddr::from(([127, 0, 0, 1], port)) }
