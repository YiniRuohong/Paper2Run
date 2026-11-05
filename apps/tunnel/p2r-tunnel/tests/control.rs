mod common;

use std::{sync::{Arc, Mutex}, time::{Duration, Instant}};

use p2r_tunnel::{
    connect::{self, Status},
    control::{self, ControlFile, ControlState},
    endpoint::peer_addr,
    pair::{self, Pairing},
    rendezvous,
    serve::{self, Registry},
};
use serde_json::Value;

struct Desk {
    net: common::TestNet,
    rv_id: iroh::EndpointId,
    desktop_id: iroh::EndpointId,
    base: String,
    token: String,
    backend: common::Backend,
    _dir: tempfile::TempDir,
}

async fn desk() -> Desk {
    let net = common::test_net(true).await;
    let rv = common::endpoint(&net).await;
    let rv_id = rv.id();
    tokio::spawn(rendezvous::serve(rv));
    let backend = common::backend().await;
    let dir = tempfile::tempdir().unwrap();
    let devices = dir.path().join("devices.json");
    let ep = common::endpoint(&net).await;
    let pairing = Pairing::new(devices.clone());
    let registry = Registry::default();
    tokio::spawn(serve::run_full(ep.clone(), backend.port, devices.clone(), Some(pairing.clone()), registry.clone()));
    let token = control::new_token();
    let port = control::spawn(ControlState {
        ep: ep.clone(), cfg: net.cfg.clone(), rendezvous: rv_id, pairing, devices, registry, token: token.clone(),
    }).await.unwrap();
    Desk { desktop_id: ep.id(), net, rv_id, base: format!("http://127.0.0.1:{port}"), token, backend, _dir: dir }
}

fn http() -> reqwest::Client { reqwest::Client::new() }

async fn call(d: &Desk, method: reqwest::Method, path: &str) -> (u16, Value) {
    let r = http().request(method, format!("{}{path}", d.base)).bearer_auth(&d.token).send().await.unwrap();
    let status = r.status().as_u16();
    let body = r.text().await.unwrap();
    (status, serde_json::from_str(&body).unwrap_or(Value::Null))
}

async fn pair_tablet(d: &Desk, name: &str) -> iroh::Endpoint {
    let (_, started) = call(d, reqwest::Method::POST, "/pairing").await;
    let code: pair::Code = started["code"].as_str().unwrap().parse().unwrap();
    let tablet = common::endpoint(&d.net).await;
    pair::join(&tablet, &d.net.cfg, d.rv_id, &code, name).await.unwrap();
    tablet
}

#[tokio::test]
async fn control_requires_token() {
    let d = desk().await;
    assert_eq!(http().get(format!("{}/status", d.base)).send().await.unwrap().status(), 401);
    assert_eq!(http().get(format!("{}/status", d.base)).bearer_auth("wrong").send().await.unwrap().status(), 401);
    let (s, body) = call(&d, reqwest::Method::GET, "/status").await;
    assert_eq!(s, 200);
    assert_eq!(body["desktop_id"], d.desktop_id.to_string());
    assert_eq!(body["devices"], serde_json::json!([]));
    assert_eq!(new_token_len(), 64);
}

fn new_token_len() -> usize { control::new_token().len() }

#[tokio::test]
async fn control_pairing_flow() {
    let d = desk().await;
    assert_eq!(call(&d, reqwest::Method::GET, "/pairing").await.1["state"], "none");
    let (s, started) = call(&d, reqwest::Method::POST, "/pairing").await;
    assert_eq!(s, 200);
    assert!(started["code"].as_str().unwrap().len() == 9 && started["expires_in_ms"].as_u64().unwrap() > 0);
    assert_eq!(call(&d, reqwest::Method::GET, "/pairing").await.1["state"], "waiting");
    let code: pair::Code = started["code"].as_str().unwrap().parse().unwrap();
    let tablet = common::endpoint(&d.net).await;
    pair::join(&tablet, &d.net.cfg, d.rv_id, &code, "Tab").await.unwrap();
    let state = call(&d, reqwest::Method::GET, "/pairing").await.1;
    assert_eq!(state["state"], "paired");
    assert_eq!(state["device"]["name"], "Tab");
    // Bring the tunnel up and check the device shows online.
    let listen = common::free_port();
    tokio::spawn(connect::run(tablet.clone(), peer_addr(&d.net.cfg, d.desktop_id), common::socket(listen)));
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let _ = http().get(format!("http://127.0.0.1:{listen}/healthz")).send().await;
        let status = call(&d, reqwest::Method::GET, "/status").await.1;
        let dev = &status["devices"][0];
        if dev["online"] == true {
            assert_eq!(dev["id"], tablet.id().to_string());
            assert_eq!(dev["name"], "Tab");
            assert!(dev["path"] == "relay" || dev["path"] == "direct", "{dev}");
            break;
        }
        assert!(Instant::now() < deadline, "device never showed online: {status}");
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

#[tokio::test]
async fn revoke_via_control_cuts_live_connection() {
    let d = desk().await;
    let tablet = pair_tablet(&d, "Tab").await;
    let conn = tablet.connect(peer_addr(&d.net.cfg, d.desktop_id), p2r_tunnel::endpoint::ALPN).await.unwrap();
    let mut tx = common::http_over_stream(&conn).await.unwrap();
    assert_eq!(tx.send_request(common::get("/healthz", &[("host", "127.0.0.1:1")])).await.unwrap().status(), 200);
    let (s, _) = call(&d, reqwest::Method::DELETE, &format!("/devices/{}", tablet.id())).await;
    assert_eq!(s, 204);
    let reason = tokio::time::timeout(Duration::from_secs(5), conn.closed()).await.expect("connection not cut");
    assert!(format!("{reason:?}").contains("revoked"), "{reason:?}");
    assert_eq!(call(&d, reqwest::Method::GET, "/status").await.1["devices"], serde_json::json!([]));
    assert_eq!(call(&d, reqwest::Method::DELETE, &format!("/devices/{}", tablet.id())).await.0, 404);
}

#[tokio::test]
async fn serve_overrides_spoofed_remote_header() {
    let d = desk().await;
    let tablet = pair_tablet(&d, "Tab").await;
    let conn = tablet.connect(peer_addr(&d.net.cfg, d.desktop_id), p2r_tunnel::endpoint::ALPN).await.unwrap();
    let mut tx = common::http_over_stream(&conn).await.unwrap();
    let resp = tx.send_request(common::get("/echo-headers", &[("host", "127.0.0.1:1"), ("x-p2r-remote", "0")])).await.unwrap();
    let text = String::from_utf8(http_body_util::BodyExt::collect(resp.into_body()).await.unwrap().to_bytes().to_vec()).unwrap();
    assert!(text.contains("x-p2r-remote=1\n"), "{text}");
    let _ = &d.backend;
}

#[tokio::test]
async fn connect_reports_revoked() {
    let d = desk().await;
    let tablet = pair_tablet(&d, "Tab").await;
    let seen = Arc::new(Mutex::new(Vec::<Status>::new()));
    let sink = seen.clone();
    let listen = common::free_port();
    let task = tokio::spawn(connect::run_with_status(tablet.clone(), peer_addr(&d.net.cfg, d.desktop_id), common::socket(listen), move |s| sink.lock().unwrap().push(s)));
    let deadline = Instant::now() + Duration::from_secs(15);
    while !seen.lock().unwrap().iter().any(|s| matches!(s, Status::Connected(_))) {
        assert!(Instant::now() < deadline, "never connected: {:?}", seen.lock().unwrap());
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    call(&d, reqwest::Method::DELETE, &format!("/devices/{}", tablet.id())).await;
    let res = tokio::time::timeout(Duration::from_secs(10), task).await.expect("connect kept running after revoke").unwrap();
    assert!(res.unwrap_err().downcast_ref::<connect::Revoked>().is_some());
    assert_eq!(seen.lock().unwrap().last(), Some(&Status::Revoked));
}

#[tokio::test]
async fn never_paired_device_is_told_revoked() {
    let d = desk().await;
    let stranger = common::endpoint(&d.net).await;
    let res = tokio::time::timeout(Duration::from_secs(15), connect::run_with_status(stranger, peer_addr(&d.net.cfg, d.desktop_id), common::socket(common::free_port()), |_| {}))
        .await.expect("stranger kept retrying");
    assert!(res.unwrap_err().downcast_ref::<connect::Revoked>().is_some());
}

#[test]
fn control_file_written_0600_and_removed_on_drop() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("control.json");
    let guard = ControlFile::write(&p, 4242, "abc").unwrap();
    let v: Value = serde_json::from_slice(&std::fs::read(&p).unwrap()).unwrap();
    assert_eq!((v["port"].as_u64(), v["token"].as_str()), (Some(4242), Some("abc")));
    assert_eq!(v["pid"].as_u64(), Some(std::process::id() as u64));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&p).unwrap().permissions().mode() & 0o777, 0o600);
    }
    drop(guard);
    assert!(!p.exists());
}
