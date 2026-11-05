mod common;

use std::{collections::HashSet, time::Duration};

use p2r_tunnel::{endpoint::peer_addr, rendezvous};

async fn rv_with_ttl(net: &common::TestNet, ttl: Duration) -> iroh::EndpointAddr {
    let ep = common::endpoint(net).await;
    let addr = peer_addr(&net.cfg, ep.id());
    tokio::spawn(rendezvous::serve_with_ttl(ep, ttl));
    addr
}

#[tokio::test]
async fn claim_then_lookup_returns_desktop_id() {
    let net = common::test_net(true).await;
    let rv = rv_with_ttl(&net, rendezvous::DEFAULT_TTL).await;
    let desktop = common::endpoint(&net).await;
    let tablet = common::endpoint(&net).await;
    let claim = rendezvous::claim(&desktop, rv.clone()).await.unwrap();
    assert!(claim.nameplate <= 999);
    assert_eq!(rendezvous::lookup(&tablet, rv, claim.nameplate).await.unwrap(), desktop.id());
}

#[tokio::test]
async fn lookup_unknown_nameplate_fails() {
    let net = common::test_net(true).await;
    let rv = rv_with_ttl(&net, rendezvous::DEFAULT_TTL).await;
    let tablet = common::endpoint(&net).await;
    assert!(rendezvous::lookup(&tablet, rv, 123).await.is_err());
}

#[tokio::test]
async fn nameplate_released_after_lookup() {
    let net = common::test_net(true).await;
    let rv = rv_with_ttl(&net, rendezvous::DEFAULT_TTL).await;
    let desktop = common::endpoint(&net).await;
    let tablet = common::endpoint(&net).await;
    let claim = rendezvous::claim(&desktop, rv.clone()).await.unwrap();
    rendezvous::lookup(&tablet, rv.clone(), claim.nameplate).await.unwrap();
    assert!(rendezvous::lookup(&tablet, rv, claim.nameplate).await.is_err());
}

#[tokio::test]
async fn nameplate_released_when_claimer_disconnects() {
    let net = common::test_net(true).await;
    let rv = rv_with_ttl(&net, rendezvous::DEFAULT_TTL).await;
    let desktop = common::endpoint(&net).await;
    let tablet = common::endpoint(&net).await;
    let claim = rendezvous::claim(&desktop, rv.clone()).await.unwrap();
    let np = claim.nameplate;
    drop(claim);
    desktop.close().await;
    let mut gone = false;
    for _ in 0..30 {
        if rendezvous::lookup(&tablet, rv.clone(), np).await.is_err() { gone = true; break; }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(gone, "nameplate still resolvable after claimer left");
}

#[tokio::test]
async fn nameplate_expires() {
    let net = common::test_net(true).await;
    let rv = rv_with_ttl(&net, Duration::from_millis(500)).await;
    let desktop = common::endpoint(&net).await;
    let tablet = common::endpoint(&net).await;
    let claim = rendezvous::claim(&desktop, rv.clone()).await.unwrap();
    tokio::time::sleep(Duration::from_millis(900)).await;
    assert!(rendezvous::lookup(&tablet, rv, claim.nameplate).await.is_err());
}

#[tokio::test]
async fn nameplates_are_unique_while_active() {
    let net = common::test_net(true).await;
    let rv = rv_with_ttl(&net, rendezvous::DEFAULT_TTL).await;
    let desktop = common::endpoint(&net).await;
    let mut claims = Vec::new();
    for _ in 0..50 { claims.push(rendezvous::claim(&desktop, rv.clone()).await.unwrap()); }
    let set: HashSet<u16> = claims.iter().map(|c| c.nameplate).collect();
    assert_eq!(set.len(), 50);
}

/// Against the deployed rendezvous (apps/tunnel/deploy/remote.json):
///   cargo test --test rendezvous -- --ignored real_rendezvous_roundtrip
#[tokio::test]
#[ignore]
async fn real_rendezvous_roundtrip() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../deploy");
    let remote: serde_json::Value = serde_json::from_slice(&std::fs::read(dir.join("remote.json")).unwrap()).unwrap();
    let cfg = p2r_tunnel::endpoint::NetConfig {
        relay_url: remote["relay_url"].as_str().unwrap().parse().unwrap(),
        relay_quic_port: remote["relay_quic_port"].as_u64().map(|p| p as u16),
        relay_ca_pem: Some(std::fs::read(dir.join("relay-ca.pem")).unwrap()),
        relay_only: false,
    };
    let rv_id: iroh::EndpointId = remote["rendezvous_id"].as_str().unwrap().parse().unwrap();
    let desktop = p2r_tunnel::endpoint::bind(&cfg, iroh::SecretKey::generate()).await.unwrap();
    let tablet = p2r_tunnel::endpoint::bind(&cfg, iroh::SecretKey::generate()).await.unwrap();
    let claim = tokio::time::timeout(Duration::from_secs(20), rendezvous::claim(&desktop, peer_addr(&cfg, rv_id))).await.unwrap().unwrap();
    let got = rendezvous::lookup(&tablet, peer_addr(&cfg, rv_id), claim.nameplate).await.unwrap();
    assert_eq!(got, desktop.id());
}
