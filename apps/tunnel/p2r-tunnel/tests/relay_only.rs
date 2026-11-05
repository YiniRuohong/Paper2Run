mod common;

use std::time::Duration;

use p2r_tunnel::endpoint::{peer_addr, ALPN};

#[tokio::test]
async fn connects_via_relay_without_discovery() {
    let net = common::test_net(true).await;
    let a = common::endpoint(&net).await;
    let b = common::endpoint(&net).await;
    let a_id = a.id();
    let server = tokio::spawn(async move {
        let conn = a.accept().await.unwrap().await.unwrap();
        let (mut send, mut recv) = conn.accept_bi().await.unwrap();
        let msg = recv.read_to_end(64).await.unwrap();
        assert_eq!(msg, b"ping");
        send.write_all(b"pong").await.unwrap();
        send.finish().unwrap();
        conn.closed().await;
    });
    let conn = tokio::time::timeout(Duration::from_secs(15), b.connect(peer_addr(&net.cfg, a_id), ALPN))
        .await.unwrap().unwrap();
    let (mut send, mut recv) = conn.open_bi().await.unwrap();
    send.write_all(b"ping").await.unwrap();
    send.finish().unwrap();
    assert_eq!(recv.read_to_end(64).await.unwrap(), b"pong");
    conn.close(0u32.into(), b"done");
    server.await.unwrap();
}

#[tokio::test]
async fn no_default_address_lookup() {
    // Without address lookup an id alone cannot be resolved: dialing it must fail.
    let net = common::test_net(true).await;
    let a = common::endpoint(&net).await;
    let b = common::endpoint(&net).await;
    let res = tokio::time::timeout(Duration::from_secs(15), b.connect(iroh::EndpointAddr::new(a.id()), ALPN)).await;
    assert!(matches!(res, Ok(Err(_))), "dialing a bare id must fail fast without discovery");
}

/// Against the real deployed relay:
/// P2R_RELAY_URL=https://60.204.202.153:12234 P2R_RELAY_CA=../deploy/relay-ca.pem \
///   cargo test --test relay_only -- --ignored real_relay_roundtrip
#[tokio::test]
#[ignore]
async fn real_relay_roundtrip() {
    let cfg = p2r_tunnel::endpoint::NetConfig {
        relay_url: std::env::var("P2R_RELAY_URL").expect("P2R_RELAY_URL").parse().unwrap(),
        relay_quic_port: Some(12234),
        relay_ca_pem: Some(std::fs::read(std::env::var("P2R_RELAY_CA").expect("P2R_RELAY_CA")).unwrap()),
        relay_only: true,
    };
    let a = p2r_tunnel::endpoint::bind(&cfg, iroh::SecretKey::generate()).await.unwrap();
    let b = p2r_tunnel::endpoint::bind(&cfg, iroh::SecretKey::generate()).await.unwrap();
    let a_id = a.id();
    tokio::spawn(async move {
        let conn = a.accept().await.unwrap().await.unwrap();
        let (mut send, mut recv) = conn.accept_bi().await.unwrap();
        let msg = recv.read_to_end(64).await.unwrap();
        send.write_all(&msg).await.unwrap();
        send.finish().unwrap();
        conn.closed().await;
    });
    let conn = tokio::time::timeout(Duration::from_secs(20), b.connect(peer_addr(&cfg, a_id), ALPN))
        .await.expect("timed out reaching real relay").unwrap();
    let (mut send, mut recv) = conn.open_bi().await.unwrap();
    send.write_all(b"hello relay").await.unwrap();
    send.finish().unwrap();
    assert_eq!(recv.read_to_end(64).await.unwrap(), b"hello relay");
}
