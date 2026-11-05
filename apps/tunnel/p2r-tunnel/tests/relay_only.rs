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
