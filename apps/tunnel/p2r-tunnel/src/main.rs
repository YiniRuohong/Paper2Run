use std::path::PathBuf;

use clap::{Args, Parser, Subcommand};
use p2r_tunnel::{endpoint, keys, serve};

#[derive(Parser)]
#[command(name = "p2r-tunnel", about = "Paper2Run tablet tunnel over iroh")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Args)]
struct Net {
    /// Private key file (created with mode 0600 if missing).
    #[arg(long)]
    key: PathBuf,
    /// Self-hosted relay URL, e.g. https://60.204.202.153:12234
    #[arg(long)]
    relay: iroh::RelayUrl,
    /// UDP port of the relay's QUIC address discovery.
    #[arg(long)]
    relay_quic_port: Option<u16>,
    /// PEM file with the relay's self-signed CA.
    #[arg(long)]
    relay_ca: Option<PathBuf>,
    /// Never use direct IP paths (forces relay; for testing fallback).
    #[arg(long)]
    relay_only: bool,
}

impl Net {
    async fn bind(&self) -> anyhow::Result<iroh::Endpoint> {
        let cfg = self.config()?;
        endpoint::bind(&cfg, keys::load_or_create(&self.key)?).await
    }

    fn config(&self) -> anyhow::Result<endpoint::NetConfig> {
        Ok(endpoint::NetConfig {
            relay_url: self.relay.clone(),
            relay_quic_port: self.relay_quic_port,
            relay_ca_pem: self.relay_ca.as_ref().map(std::fs::read).transpose()?,
            relay_only: self.relay_only,
        })
    }
}

#[derive(Subcommand)]
enum Cmd {
    /// Print this device's public id (creating the key if needed).
    Id {
        #[arg(long)]
        key: PathBuf,
    },
    /// Desktop side: forward allow-listed peers to the local backend.
    Serve {
        #[command(flatten)]
        net: Net,
        #[arg(long)]
        backend_port: u16,
        /// File with one allowed peer id per line.
        #[arg(long)]
        allow: PathBuf,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "p2r_tunnel=info".into()))
        .init();
    match Cli::parse().cmd {
        Cmd::Id { key } => println!("{}", keys::load_or_create(&key)?.public()),
        Cmd::Serve { net, backend_port, allow } => {
            let ep = net.bind().await?;
            tracing::info!("serving id={} backend=127.0.0.1:{backend_port}", ep.id());
            serve::run(ep, backend_port, allow).await?;
        }
    }
    Ok(())
}
