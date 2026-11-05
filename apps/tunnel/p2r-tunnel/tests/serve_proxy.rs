mod common;

use std::{sync::atomic::Ordering, time::{Duration, Instant}};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use p2r_tunnel::endpoint::{peer_addr, ALPN};
use sha2::{Digest, Sha256};

struct Rig {
    net: common::TestNet,
    server_id: iroh::EndpointId,
    allow: std::path::PathBuf,
    _dir: tempfile::TempDir,
}

async fn rig(backend_port: u16, allow_client: bool, client: &iroh::Endpoint, net: common::TestNet) -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let allow = dir.path().join("allowed");
    let ids = if allow_client { vec![client.id()] } else { vec![] };
    common::write_allow(&allow, &ids);
    let server = common::endpoint(&net).await;
    let server_id = server.id();
    tokio::spawn(p2r_tunnel::serve::run(server, backend_port, allow.clone()));
    Rig { net, server_id, allow, _dir: dir }
}

async fn connect(client: &iroh::Endpoint, rig: &Rig) -> anyhow::Result<iroh::endpoint::Connection> {
    Ok(tokio::time::timeout(Duration::from_secs(15), client.connect(peer_addr(&rig.net.cfg, rig.server_id), ALPN)).await??)
}

async fn body_text(resp: hyper::Response<hyper::body::Incoming>) -> String {
    String::from_utf8(resp.into_body().collect().await.unwrap().to_bytes().to_vec()).unwrap()
}

#[tokio::test]
async fn rejects_peer_not_in_allowlist() {
    let backend = common::backend().await;
    let net = common::test_net(true).await;
    let client = common::endpoint(&net).await;
    let rig = rig(backend.port, false, &client, net).await;
    let outcome = async {
        let conn = connect(&client, &rig).await?;
        let mut tx = common::http_over_stream(&conn).await?;
        let resp = tokio::time::timeout(Duration::from_secs(5), tx.send_request(common::get("/healthz", &[]))).await??;
        anyhow::Ok(resp.status())
    }.await;
    assert!(outcome.is_err(), "non-allowlisted peer got a response: {outcome:?}");
    assert_eq!(backend.hits.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn rewrites_host_and_origin() {
    let backend = common::backend().await;
    let net = common::test_net(true).await;
    let client = common::endpoint(&net).await;
    let rig = rig(backend.port, true, &client, net).await;
    let conn = connect(&client, &rig).await.unwrap();
    let mut tx = common::http_over_stream(&conn).await.unwrap();
    let resp = tx.send_request(common::get("/echo-headers", &[
        ("host", "127.0.0.1:18080"), ("origin", "http://127.0.0.1:18080"), ("sec-fetch-site", "same-origin"),
    ])).await.unwrap();
    let text = body_text(resp).await;
    let p = backend.port;
    assert_eq!(text, format!("host=127.0.0.1:{p}\norigin=http://127.0.0.1:{p}\nsec-fetch-site=same-origin\n"));

    let resp = tx.send_request(common::get("/echo-headers", &[("host", "127.0.0.1:18080")])).await.unwrap();
    assert_eq!(body_text(resp).await, format!("host=127.0.0.1:{p}\norigin=-\nsec-fetch-site=-\n"));
}

#[tokio::test]
async fn sse_chunks_arrive_before_stream_ends() {
    let backend = common::backend().await;
    let net = common::test_net(true).await;
    let client = common::endpoint(&net).await;
    let rig = rig(backend.port, true, &client, net).await;
    let conn = connect(&client, &rig).await.unwrap();
    let mut tx = common::http_over_stream(&conn).await.unwrap();
    let start = Instant::now();
    let mut body = tx.send_request(common::get("/sse", &[("host", "127.0.0.1:1")])).await.unwrap().into_body();
    let mut events = 0;
    while start.elapsed() < Duration::from_millis(700) {
        match tokio::time::timeout(Duration::from_millis(700) - start.elapsed().min(Duration::from_millis(700)), body.frame()).await {
            Ok(Some(Ok(f))) => if let Some(d) = f.data_ref() { events += d.windows(5).filter(|w| w == b"data:").count(); },
            _ => break,
        }
    }
    assert!(events >= 2, "only {events} SSE events within 700ms");
}

#[tokio::test]
async fn large_upload_streams_through() {
    let backend = common::backend().await;
    let net = common::test_net(true).await;
    let client = common::endpoint(&net).await;
    let rig = rig(backend.port, true, &client, net).await;
    let conn = connect(&client, &rig).await.unwrap();
    let mut tx = common::http_over_stream(&conn).await.unwrap();
    let data: Vec<u8> = (0..16 * 1024 * 1024u32).map(|i| (i % 251) as u8).collect();
    let expected = common::hex(&Sha256::digest(&data));
    let req = hyper::Request::post("/upload").header("host", "127.0.0.1:1")
        .body(Full::new(Bytes::from(data)).map_err(|e| match e {}).boxed()).unwrap();
    let resp = tokio::time::timeout(Duration::from_secs(60), tx.send_request(req)).await.unwrap().unwrap();
    assert_eq!(body_text(resp).await, expected);
}

#[tokio::test]
async fn backend_down_returns_502() {
    let net = common::test_net(true).await;
    let client = common::endpoint(&net).await;
    let rig = rig(common::free_port(), true, &client, net).await;
    let conn = connect(&client, &rig).await.unwrap();
    let mut tx = common::http_over_stream(&conn).await.unwrap();
    let resp = tokio::time::timeout(Duration::from_secs(10), tx.send_request(common::get("/healthz", &[("host", "127.0.0.1:1")]))).await.unwrap().unwrap();
    assert_eq!(resp.status(), 502);
}

#[tokio::test]
async fn allowlist_reloaded_per_connection() {
    let backend = common::backend().await;
    let net = common::test_net(true).await;
    let client = common::endpoint(&net).await;
    let rig = rig(backend.port, false, &client, net).await;
    let first = async {
        let conn = connect(&client, &rig).await?;
        let mut tx = common::http_over_stream(&conn).await?;
        anyhow::Ok(tokio::time::timeout(Duration::from_secs(5), tx.send_request(common::get("/healthz", &[("host", "127.0.0.1:1")]))).await??.status())
    }.await;
    assert!(first.is_err());
    common::write_allow(&rig.allow, &[client.id()]);
    // A fresh client endpoint with the same key forces a brand-new connection.
    let client2 = common::endpoint_with(&rig.net, client.secret_key().clone()).await;
    client.close().await;
    let conn = connect(&client2, &rig).await.unwrap();
    let mut tx = common::http_over_stream(&conn).await.unwrap();
    let resp = tx.send_request(common::get("/healthz", &[("host", "127.0.0.1:1")])).await.unwrap();
    assert_eq!(resp.status(), 200);
}

#[test]
fn allow_file_parsing() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("a");
    let id = iroh::SecretKey::generate().public();
    std::fs::write(&p, format!("# c\n\n  {id}  \n")).unwrap();
    let set = p2r_tunnel::allow::load(&p).unwrap();
    assert!(set.contains(&id) && set.len() == 1);
    assert!(p2r_tunnel::allow::load(&dir.path().join("missing")).unwrap().is_empty());
    std::fs::write(&p, "not-a-key\n").unwrap();
    assert!(p2r_tunnel::allow::load(&p).is_err());
}


async fn status_for(headers: &[(&str, &str)], method: &str, uri: &str) -> (u16, usize) {
    let backend = common::backend().await;
    let net = common::test_net(true).await;
    let client = common::endpoint(&net).await;
    let rig = rig(backend.port, true, &client, net).await;
    let conn = connect(&client, &rig).await.unwrap();
    let mut tx = common::http_over_stream(&conn).await.unwrap();
    let mut b = hyper::Request::builder().method(method).uri(uri);
    for (k, v) in headers { b = b.header(*k, *v); }
    let req = b.body(Full::new(Bytes::new()).map_err(|e| match e {}).boxed()).unwrap();
    let resp = tx.send_request(req).await.unwrap();
    (resp.status().as_u16(), backend.hits.load(Ordering::SeqCst))
}

#[tokio::test]
async fn rejects_rebound_host() {
    assert_eq!(status_for(&[("host", "evil.example:18080")], "GET", "/healthz").await, (421, 0));
}

#[tokio::test]
async fn rejects_foreign_origin() {
    let h = [("host", "127.0.0.1:18080"), ("origin", "http://evil.example")];
    assert_eq!(status_for(&h, "POST", "/healthz").await, (421, 0));
}

#[tokio::test]
async fn rejects_cross_site_fetch() {
    let h = [("host", "127.0.0.1:18080"), ("sec-fetch-site", "cross-site")];
    assert_eq!(status_for(&h, "POST", "/healthz").await, (421, 0));
}

#[tokio::test]
async fn accepts_localhost_host() {
    let h = [("host", "localhost:18080"), ("origin", "http://localhost:18080"), ("sec-fetch-site", "same-origin")];
    assert_eq!(status_for(&h, "GET", "/healthz").await.0, 200);
}

#[tokio::test]
async fn absolute_form_uri_still_hits_backend() {
    let h = [("host", "127.0.0.1:18080")];
    assert_eq!(status_for(&h, "GET", "http://127.0.0.1:1/healthz").await, (200, 1));
}

#[tokio::test]
async fn revoked_peer_is_cut_on_live_connection() {
    let backend = common::backend().await;
    let net = common::test_net(true).await;
    let client = common::endpoint(&net).await;
    let rig = rig(backend.port, true, &client, net).await;
    let conn = connect(&client, &rig).await.unwrap();
    let mut tx = common::http_over_stream(&conn).await.unwrap();
    assert_eq!(tx.send_request(common::get("/healthz", &[("host", "127.0.0.1:1")])).await.unwrap().status(), 200);
    common::write_allow(&rig.allow, &[]);
    let hits_before = backend.hits.load(Ordering::SeqCst);
    let again = async {
        let mut tx = common::http_over_stream(&conn).await?;
        anyhow::Ok(tokio::time::timeout(Duration::from_secs(5), tx.send_request(common::get("/healthz", &[("host", "127.0.0.1:1")]))).await??.status())
    }.await;
    assert!(again.is_err(), "revoked peer still served: {again:?}");
    assert_eq!(backend.hits.load(Ordering::SeqCst), hits_before);
}
