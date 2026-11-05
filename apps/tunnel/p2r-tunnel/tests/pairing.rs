mod common;

use std::{sync::atomic::Ordering, time::Duration};

use p2r_tunnel::{
    devices,
    endpoint::peer_addr,
    pair::{self, Code, JoinError, PairState, Pairing},
    rendezvous,
};

struct Desk {
    net: common::TestNet,
    rv_id: iroh::EndpointId,
    desktop: iroh::Endpoint,
    pairing: Pairing,
    devices: std::path::PathBuf,
    backend: common::Backend,
    _dir: tempfile::TempDir,
}

async fn desk(ttl: Duration) -> Desk {
    let net = common::test_net(true).await;
    let rv = common::endpoint(&net).await;
    let rv_id = rv.id();
    tokio::spawn(rendezvous::serve(rv));
    let backend = common::backend().await;
    let dir = tempfile::tempdir().unwrap();
    let devices = dir.path().join("devices.json");
    let desktop = common::endpoint(&net).await;
    let pairing = Pairing::with_ttl(devices.clone(), ttl);
    tokio::spawn(p2r_tunnel::serve::run_with(desktop.clone(), backend.port, devices.clone(), Some(pairing.clone())));
    Desk { net, rv_id, desktop, pairing, devices, backend, _dir: dir }
}

async fn join(d: &Desk, tablet: &iroh::Endpoint, code: &Code, name: &str) -> anyhow::Result<iroh::EndpointId> {
    tokio::time::timeout(Duration::from_secs(20), pair::join(tablet, &d.net.cfg, d.rv_id, code, name)).await?
}

#[test]
fn code_parsing() {
    let c: Code = "123-45678".parse().unwrap();
    assert_eq!(c, Code { nameplate: 123, secret: 45678 });
    assert_eq!("12345678".parse::<Code>().unwrap(), c);
    assert_eq!(" 123 45678 ".parse::<Code>().unwrap(), c);
    assert_eq!(Code { nameplate: 7, secret: 42 }.to_string(), "007-00042");
    for bad in ["1234567", "123456789", "12a-45678", ""] { assert!(bad.parse::<Code>().is_err(), "{bad}"); }
}

#[tokio::test]
async fn pairs_with_correct_code() {
    let d = desk(pair::CODE_TTL).await;
    let code = d.pairing.start(&d.desktop, &d.net.cfg, d.rv_id).await.unwrap();
    assert!(matches!(d.pairing.state().await, PairState::Waiting { .. }));
    let tablet = common::endpoint(&d.net).await;
    let got = join(&d, &tablet, &code, "Lenovo TB522FU").await.unwrap();
    assert_eq!(got, d.desktop.id());
    let list = devices::list(&d.devices).unwrap();
    assert_eq!(list.len(), 1);
    assert_eq!((list[0].id, list[0].name.as_str()), (tablet.id(), "Lenovo TB522FU"));
    assert!(matches!(d.pairing.state().await, PairState::Paired { .. }));
    // The freshly paired tablet can now use the HTTP tunnel.
    let conn = tablet.connect(peer_addr(&d.net.cfg, got), p2r_tunnel::endpoint::ALPN).await.unwrap();
    let mut tx = common::http_over_stream(&conn).await.unwrap();
    let resp = tx.send_request(common::get("/healthz", &[("host", "127.0.0.1:1")])).await.unwrap();
    assert_eq!(resp.status(), 200);
    assert!(d.backend.hits.load(Ordering::SeqCst) >= 1);
}

#[tokio::test]
async fn wrong_secret_burns_code() {
    let d = desk(pair::CODE_TTL).await;
    let code = d.pairing.start(&d.desktop, &d.net.cfg, d.rv_id).await.unwrap();
    let wrong = Code { nameplate: code.nameplate, secret: (code.secret + 1) % 100_000 };
    let tablet = common::endpoint(&d.net).await;
    let err = join(&d, &tablet, &wrong, "t").await.unwrap_err();
    assert_eq!(err.downcast_ref::<JoinError>(), Some(&JoinError::WrongCode), "{err:#}");
    assert_eq!(d.pairing.state().await, PairState::WrongCode);
    let again = join(&d, &tablet, &code, "t").await.unwrap_err();
    assert_eq!(again.downcast_ref::<JoinError>(), Some(&JoinError::NoSuchCode), "{again:#}");
    assert!(devices::list(&d.devices).unwrap().is_empty());
}

#[tokio::test]
async fn code_is_single_use() {
    let d = desk(pair::CODE_TTL).await;
    let code = d.pairing.start(&d.desktop, &d.net.cfg, d.rv_id).await.unwrap();
    let first = common::endpoint(&d.net).await;
    join(&d, &first, &code, "first").await.unwrap();
    let second = common::endpoint(&d.net).await;
    let err = join(&d, &second, &code, "second").await.unwrap_err();
    assert_eq!(err.downcast_ref::<JoinError>(), Some(&JoinError::NoSuchCode), "{err:#}");
    let list = devices::list(&d.devices).unwrap();
    assert_eq!(list.iter().map(|x| x.name.as_str()).collect::<Vec<_>>(), ["first"]);
}

#[tokio::test]
async fn code_expires() {
    let d = desk(Duration::from_secs(1)).await;
    let code = d.pairing.start(&d.desktop, &d.net.cfg, d.rv_id).await.unwrap();
    tokio::time::sleep(Duration::from_secs(2)).await;
    let tablet = common::endpoint(&d.net).await;
    let err = join(&d, &tablet, &code, "t").await.unwrap_err();
    assert_eq!(err.downcast_ref::<JoinError>(), Some(&JoinError::NoSuchCode), "{err:#}");
    assert_eq!(d.pairing.state().await, PairState::Expired);
}

#[tokio::test]
async fn new_session_voids_previous_code() {
    let d = desk(pair::CODE_TTL).await;
    let old = d.pairing.start(&d.desktop, &d.net.cfg, d.rv_id).await.unwrap();
    let new = d.pairing.start(&d.desktop, &d.net.cfg, d.rv_id).await.unwrap();
    let tablet = common::endpoint(&d.net).await;
    if old.nameplate != new.nameplate {
        assert!(join(&d, &tablet, &old, "t").await.is_err());
    }
    join(&d, &tablet, &new, "t").await.unwrap();
}

#[test]
fn devices_file_roundtrip_and_0600() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("sub").join("devices.json");
    assert!(devices::list(&p).unwrap().is_empty());
    let a = devices::Device { id: iroh::SecretKey::generate().public(), name: "a".into(), paired_at: 1 };
    let b = devices::Device { id: iroh::SecretKey::generate().public(), name: "b".into(), paired_at: 2 };
    devices::add(&p, a.clone()).unwrap();
    devices::add(&p, b.clone()).unwrap();
    devices::add(&p, devices::Device { name: "a2".into(), ..a.clone() }).unwrap();
    let list = devices::list(&p).unwrap();
    assert_eq!(list.len(), 2);
    assert_eq!(list.iter().find(|d| d.id == a.id).unwrap().name, "a2");
    assert!(devices::remove(&p, a.id).unwrap());
    assert!(!devices::remove(&p, a.id).unwrap());
    assert_eq!(devices::list(&p).unwrap(), vec![b]);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&p).unwrap().permissions().mode() & 0o777, 0o600);
    }
}
