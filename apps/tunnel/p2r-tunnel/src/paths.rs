//! Honest reporting of which network path a connection is actually using.

use std::time::Duration;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PathKind {
    Direct,
    Relay,
    Unknown,
}

impl std::fmt::Display for PathKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            PathKind::Direct => "direct",
            PathKind::Relay => "relay",
            PathKind::Unknown => "unknown",
        })
    }
}

/// The selected path as reported by iroh; never inferred.
pub fn selected(conn: &iroh::endpoint::Connection) -> (PathKind, Option<Duration>) {
    for path in conn.paths().iter() {
        if path.is_selected() {
            let kind = if path.is_relay() { PathKind::Relay } else if path.is_ip() { PathKind::Direct } else { PathKind::Unknown };
            return (kind, Some(path.rtt()));
        }
    }
    (PathKind::Unknown, None)
}

/// Log the selected path now and every time it changes, until the connection closes.
pub fn spawn_logger(conn: iroh::endpoint::Connection) {
    tokio::spawn(async move {
        use n0_future::StreamExt;
        let peer = conn.remote_id();
        let mut last = None;
        let mut stream = conn.paths_stream();
        while stream.next().await.is_some() {
            let (kind, rtt) = selected(&conn);
            if last != Some(kind) {
                last = Some(kind);
                tracing::info!("peer={peer} path={kind} rtt_ms={}", rtt.map(|d| d.as_millis()).unwrap_or(0));
            }
        }
    });
}
