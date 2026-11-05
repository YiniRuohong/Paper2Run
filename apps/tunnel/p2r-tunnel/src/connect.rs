//! Tablet side: expose the remote backend on a local TCP port. Each local TCP
//! connection becomes one QUIC bi-stream; bytes are copied verbatim.

use std::{net::SocketAddr, time::Duration};

use iroh::{endpoint::Connection, Endpoint, EndpointAddr};
use tokio::{net::TcpStream, sync::watch};

use crate::endpoint::ALPN;

const BACKOFF_START: Duration = Duration::from_millis(500);
const BACKOFF_MAX: Duration = Duration::from_secs(10);
/// How long a local TCP connection waits for the tunnel before being dropped.
const WAIT_FOR_TUNNEL: Duration = Duration::from_secs(10);

pub async fn run(ep: Endpoint, peer: EndpointAddr, listen: SocketAddr) -> anyhow::Result<()> {
    // The desktop backend has no authentication; exposing it beyond this
    // device's loopback would hand it to everyone on the tablet's network.
    anyhow::ensure!(listen.ip().is_loopback(), "refusing to listen on non-loopback address {listen}");
    let listener = tokio::net::TcpListener::bind(listen).await?;
    tracing::info!("listening on {listen}, peer={}", peer.id);
    let (tx, rx) = watch::channel::<Option<Connection>>(None);
    tokio::spawn(maintain(ep, peer, tx));
    loop {
        let (sock, _) = listener.accept().await?;
        let rx = rx.clone();
        tokio::spawn(async move {
            if let Err(e) = pump(sock, rx).await {
                tracing::debug!("local connection ended: {e:#}");
            }
        });
    }
}

/// Keep one live connection to the peer, reconnecting with exponential backoff.
async fn maintain(ep: Endpoint, peer: EndpointAddr, tx: watch::Sender<Option<Connection>>) {
    let mut backoff = BACKOFF_START;
    loop {
        match ep.connect(peer.clone(), ALPN).await {
            Ok(conn) => {
                backoff = BACKOFF_START;
                crate::paths::spawn_logger(conn.clone());
                tx.send_replace(Some(conn.clone()));
                let reason = conn.closed().await;
                tx.send_replace(None);
                tracing::warn!("tunnel closed: {reason}; reconnecting");
            }
            Err(e) => {
                tracing::warn!("connect failed: {e}; retrying in {backoff:?}");
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(BACKOFF_MAX);
            }
        }
        if tx.is_closed() {
            return;
        }
    }
}

async fn pump(mut sock: TcpStream, mut rx: watch::Receiver<Option<Connection>>) -> anyhow::Result<()> {
    let conn = tokio::time::timeout(WAIT_FOR_TUNNEL, rx.wait_for(|c| c.is_some()))
        .await
        .map_err(|_| anyhow::anyhow!("tunnel not available"))??
        .clone()
        .expect("checked is_some");
    let (send, recv) = conn.open_bi().await?;
    let mut remote = tokio::io::join(recv, send);
    tokio::io::copy_bidirectional(&mut sock, &mut remote).await?;
    Ok(())
}
