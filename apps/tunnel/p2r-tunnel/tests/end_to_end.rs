mod common;

use std::time::{Duration, Instant};

use p2r_tunnel::{endpoint::{peer_addr, ALPN}, paths::{self, PathKind}};

struct Stack {
    listen: u16,
    _dir: tempfile::TempDir,
    _backend: common::Backend,
    net: common::TestNet,
    server_key: iroh::SecretKey,
    backend_port: u16,
    allow: std::path::PathBuf,
}

async fn stack() -> (Stack, iroh::Endpoint) {
    let backend = common::backend().await;
    let net = common::test_net(true).await;
    let dir = tempfile::tempdir().unwrap();
    let allow = dir.path().join("allowed");
    let client = common::endpoint(&net).await;
    common::write_allow(&allow, &[client.id()]);
    let server_key = iroh::SecretKey::generate();
    let server = common::endpoint_with(&net, server_key.clone()).await;
    tokio::spawn(p2r_tunnel::serve::run(server.clone(), backend.port, allow.clone()));
    let listen = common::free_port();
    let peer = peer_addr(&net.cfg, server_key.public());
    tokio::spawn(p2r_tunnel::connect::run(client, peer, common::socket(listen)));
    let backend_port = backend.port;
    (Stack { listen, _dir: dir, _backend: backend, net, server_key, backend_port, allow }, server)
}

fn url(s: &Stack, path: &str) -> String { format!("http://127.0.0.1:{}{path}", s.listen) }

async fn get_eventually(u: &str, deadline: Duration) -> reqwest::Response {
    let start = Instant::now();
    loop {
        match reqwest::Client::new().get(u).timeout(Duration::from_secs(3)).send().await {
            Ok(r) => return r,
            Err(e) if start.elapsed() < deadline => { let _ = e; tokio::time::sleep(Duration::from_millis(200)).await; }
            Err(e) => panic!("no response within {deadline:?}: {e}"),
        }
    }
}

#[tokio::test]
async fn get_through_tunnel() {
    let (s, _server) = stack().await;
    let r = get_eventually(&url(&s, "/healthz"), Duration::from_secs(15)).await;
    assert_eq!(r.status(), 200);
    assert_eq!(r.text().await.unwrap(), "ok");
}

#[tokio::test]
async fn parallel_connections_do_not_block() {
    let (s, _server) = stack().await;
    get_eventually(&url(&s, "/healthz"), Duration::from_secs(15)).await;
    let hang = reqwest::Client::new().get(url(&s, "/hang-sse")).send().await.unwrap();
    assert_eq!(hang.status(), 200);
    let start = Instant::now();
    let reqs = (0..20).map(|_| {
        let u = url(&s, "/healthz");
        tokio::spawn(async move { reqwest::Client::new().get(u).send().await.unwrap().status() })
    });
    for r in reqs { assert_eq!(r.await.unwrap(), 200); }
    assert!(start.elapsed() < Duration::from_secs(2), "took {:?}", start.elapsed());
    drop(hang);
}

#[tokio::test]
async fn connect_recovers_after_serve_restart() {
    let (s, server) = stack().await;
    get_eventually(&url(&s, "/healthz"), Duration::from_secs(15)).await;
    server.close().await;
    let server2 = common::endpoint_with(&s.net, s.server_key.clone()).await;
    tokio::spawn(p2r_tunnel::serve::run(server2, s.backend_port, s.allow.clone()));
    let r = get_eventually(&url(&s, "/healthz"), Duration::from_secs(10)).await;
    assert_eq!(r.status(), 200);
}

#[tokio::test]
async fn relay_only_reports_relay() {
    let net = common::test_net(true).await;
    let a = common::endpoint(&net).await;
    let b = common::endpoint(&net).await;
    let a_id = a.id();
    tokio::spawn(async move { let c = a.accept().await.unwrap().await.unwrap(); c.closed().await; });
    let conn = b.connect(peer_addr(&net.cfg, a_id), ALPN).await.unwrap();
    assert_eq!(paths::selected(&conn).0, PathKind::Relay);
}
