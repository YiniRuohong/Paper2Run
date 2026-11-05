//! Tablet side: expose the remote backend on a local TCP port. Each local TCP
//! connection becomes one QUIC bi-stream; bytes are copied verbatim.

use std::{net::SocketAddr, sync::Arc, time::Duration};

use iroh::{
    endpoint::{Connection, ConnectionError},
    Endpoint, EndpointAddr,
};
use tokio::{net::TcpStream, sync::watch};

use crate::{
    endpoint::ALPN,
    paths::{self, PathKind},
    serve::CLOSE_REVOKED,
};

const BACKOFF_START: Duration = Duration::from_millis(500);
const BACKOFF_MAX: Duration = Duration::from_secs(10);
/// How long a local TCP connection waits for the tunnel before being dropped.
const WAIT_FOR_TUNNEL: Duration = Duration::from_secs(10);

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Status {
    Connecting,
    Connected(PathKind),
    Revoked,
}

/// Returned when the desktop says this device is not (or no longer) paired.
#[derive(Debug)]
pub struct Revoked;
impl std::fmt::Display for Revoked {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("this device was revoked by the desktop")
    }
}
impl std::error::Error for Revoked {}

type OnStatus = Arc<dyn Fn(Status) + Send + Sync>;

pub async fn run(ep: Endpoint, peer: EndpointAddr, listen: SocketAddr) -> anyhow::Result<()> {
    run_with_status(ep, peer, listen, |_| {}).await
}

/// Like [`run`], reporting tunnel state changes. Returns `Err(Revoked)` once
/// the desktop rejects this device; it does not retry after that.
pub async fn run_with_status(ep: Endpoint, peer: EndpointAddr, listen: SocketAddr, on_status: impl Fn(Status) + Send + Sync + 'static) -> anyhow::Result<()> {
    // The desktop backend has no authentication; exposing it beyond this
    // device's loopback would hand it to everyone on the tablet's network.
    anyhow::ensure!(listen.ip().is_loopback(), "refusing to listen on non-loopback address {listen}");
    let listener = tokio::net::TcpListener::bind(listen).await?;
    tracing::info!("listening on {listen}, peer={}", peer.id);
    let on_status: OnStatus = Arc::new(on_status);
    let (tx, rx) = watch::channel::<Option<Connection>>(None);
    let maintainer = tokio::spawn(maintain(ep, peer, tx, on_status.clone()));
    let accept = async {
        loop {
            let (sock, _) = listener.accept().await?;
            let rx = rx.clone();
            tokio::spawn(async move {
                if let Err(e) = pump(sock, rx).await {
                    tracing::debug!("local connection ended: {e:#}");
                }
            });
        }
        #[allow(unreachable_code)]
        anyhow::Ok(())
    };
    tokio::select! {
        r = accept => r,
        _ = maintainer => {
            on_status(Status::Revoked);
            Err(anyhow::Error::new(Revoked))
        }
    }
}

fn is_revoked(reason: &ConnectionError) -> bool {
    matches!(reason, ConnectionError::ApplicationClosed(c) if u64::from(c.error_code) == CLOSE_REVOKED as u64)
}

/// Keep one live connection to the peer, reconnecting with exponential backoff.
/// Returns only when the desktop revokes this device.
async fn maintain(ep: Endpoint, peer: EndpointAddr, tx: watch::Sender<Option<Connection>>, on_status: OnStatus) {
    let mut backoff = BACKOFF_START;
    loop {
        on_status(Status::Connecting);
        match ep.connect(peer.clone(), ALPN).await {
            Ok(conn) => {
                backoff = BACKOFF_START;
                paths::spawn_logger(conn.clone());
                spawn_path_reporter(conn.clone(), on_status.clone());
                tx.send_replace(Some(conn.clone()));
                let reason = conn.closed().await;
                tx.send_replace(None);
                if is_revoked(&reason) {
                    tracing::warn!("desktop revoked this device");
                    return;
                }
                tracing::warn!("tunnel closed: {reason}; reconnecting");
            }
            Err(e) => {
                tracing::warn!("connect failed: {e}; retrying in {backoff:?}");
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(BACKOFF_MAX);
            }
        }
    }
}

fn spawn_path_reporter(conn: Connection, on_status: OnStatus) {
    tokio::spawn(async move {
        use n0_future::StreamExt;
        let mut last = None;
        let mut stream = conn.paths_stream();
        while stream.next().await.is_some() {
            if conn.close_reason().is_some() {
                break;
            }
            let (kind, _) = paths::selected(&conn);
            if kind != PathKind::Unknown && last != Some(kind) {
                last = Some(kind);
                on_status(Status::Connected(kind));
            }
        }
    });
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
