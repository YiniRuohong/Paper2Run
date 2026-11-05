//! Close the endpoint cleanly on a shutdown signal so peers learn immediately
//! instead of waiting for an idle timeout.

use std::future::Future;

pub async fn run_until(
    ep: iroh::Endpoint,
    signal: impl Future<Output = ()>,
    work: impl Future<Output = anyhow::Result<()>>,
) -> anyhow::Result<()> {
    let result = tokio::select! {
        r = work => r,
        _ = signal => {
            tracing::info!("shutting down");
            Ok(())
        }
    };
    ep.close().await;
    result
}

/// Ctrl-C everywhere, plus SIGTERM on unix.
pub async fn signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        if let Ok(mut term) = signal(SignalKind::terminate()) {
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {}
                _ = term.recv() => {}
            }
            return;
        }
    }
    let _ = tokio::signal::ctrl_c().await;
}
