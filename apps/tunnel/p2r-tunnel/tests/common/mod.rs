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
