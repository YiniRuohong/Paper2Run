//! An iroh endpoint that only knows one self-hosted relay and no address lookup.

use iroh::{Endpoint, EndpointAddr, EndpointId, RelayConfig, RelayMap, RelayMode, RelayUrl, SecretKey};
use iroh_relay::RelayQuicConfig;
pub use iroh_relay::tls::CaTlsConfig;

pub const ALPN: &[u8] = b"p2r/http/1";

#[derive(Clone, Debug)]
pub struct NetConfig {
    /// e.g. https://60.204.202.153:12234
    pub relay_url: RelayUrl,
    /// UDP port of the relay's QUIC address discovery, if enabled.
    pub relay_quic_port: Option<u16>,
    /// Self-signed CA to pin; None means the embedded webpki roots.
    pub relay_ca_pem: Option<Vec<u8>>,
    /// Drop IP transports so every packet goes through the relay.
    pub relay_only: bool,
}

pub async fn bind(cfg: &NetConfig, key: SecretKey) -> anyhow::Result<Endpoint> {
    let ca = match &cfg.relay_ca_pem {
        Some(pem) => {
            let roots = rustls_pemfile::certs(&mut pem.as_slice()).collect::<Result<Vec<_>, _>>()?;
            anyhow::ensure!(!roots.is_empty(), "relay CA file contains no certificate");
            CaTlsConfig::custom_roots(roots)
        }
        None => CaTlsConfig::embedded(),
    };
    bind_with(cfg, key, ca).await
}

/// Bind with an explicit TLS trust config for the relay (tests use an insecure one).
pub async fn bind_with(cfg: &NetConfig, key: SecretKey, ca: CaTlsConfig) -> anyhow::Result<Endpoint> {
    let relay = RelayConfig::new(cfg.relay_url.clone(), cfg.relay_quic_port.map(RelayQuicConfig::new));
    let mut builder = Endpoint::builder(iroh::endpoint::presets::Minimal)
        .secret_key(key)
        .alpns(vec![ALPN.to_vec()])
        .relay_mode(RelayMode::Custom(RelayMap::from(relay)))
        .clear_address_lookup()
        .ca_tls_config(ca);
    if cfg.relay_only {
        builder = builder.clear_ip_transports();
    }
    Ok(builder.bind().await?)
}

/// With a single relay, a peer is fully addressed by its id plus that relay.
pub fn peer_addr(cfg: &NetConfig, peer: EndpointId) -> EndpointAddr {
    EndpointAddr::new(peer).with_relay_url(cfg.relay_url.clone())
}
