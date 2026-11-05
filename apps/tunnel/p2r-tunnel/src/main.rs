use std::path::PathBuf;

use clap::{Args, Parser, Subcommand};
use p2r_tunnel::{connect, control, endpoint, keys, pair, rendezvous, serve, shutdown};

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
        /// Paired devices (JSON); created on first pairing.
        #[arg(long)]
        devices: PathBuf,
        /// Rendezvous endpoint id (from remote.json); enables pairing.
        #[arg(long)]
        rendezvous: Option<iroh::EndpointId>,
        /// Where to publish the control API's port and token.
        #[arg(long)]
        control_file: Option<PathBuf>,
    },
    /// Relay host: hand out pairing nameplates.
    Rendezvous {
        #[command(flatten)]
        net: Net,
    },
    /// Tablet side: pair with a desktop using its code. Exit 0 ok, 3 wrong
    /// code (now void), 4 unknown or expired code, 1 anything else.
    Pair {
        #[command(flatten)]
        net: Net,
        /// Rendezvous endpoint id (from remote.json).
        #[arg(long)]
        rendezvous: iroh::EndpointId,
        /// Pairing code shown on the desktop, e.g. 123-45678.
        #[arg(long)]
        code: pair::Code,
        /// Name shown in the desktop's device list.
        #[arg(long)]
        name: String,
        /// File to write the desktop's id to.
        #[arg(long)]
        out: PathBuf,
    },
    /// Tablet side: expose the remote backend on a local port.
    Connect {
        #[command(flatten)]
        net: Net,
        /// The desktop's peer id.
        #[arg(long)]
        peer: iroh::EndpointId,
        #[arg(long, default_value = "127.0.0.1:18080")]
        listen: std::net::SocketAddr,
        /// Print one JSON line per state change on stdout. Exits 5 when revoked.
        #[arg(long)]
        status_json: bool,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "p2r_tunnel=info".into()))
        .init();
    match Cli::parse().cmd {
        Cmd::Id { key } => println!("{}", keys::load_or_create(&key)?.public()),
        Cmd::Serve { net, backend_port, devices, rendezvous, control_file } => {
            let cfg = net.config()?;
            let ep = net.bind().await?;
            tracing::info!("serving id={} backend=127.0.0.1:{backend_port}", ep.id());
            let pairing = pair::Pairing::new(devices.clone());
            let registry = serve::Registry::default();
            let mut _control_guard = None;
            if let Some(path) = control_file {
                let rendezvous = rendezvous.ok_or_else(|| anyhow::anyhow!("--control-file needs --rendezvous"))?;
                let token = control::new_token();
                let port = control::spawn(control::ControlState {
                    ep: ep.clone(), cfg, rendezvous, pairing: pairing.clone(),
                    devices: devices.clone(), registry: registry.clone(), token: token.clone(),
                }).await?;
                _control_guard = Some(control::ControlFile::write(&path, port, &token)?);
                tracing::info!("control api on 127.0.0.1:{port}");
            }
            let work = serve::run_full(ep.clone(), backend_port, devices, rendezvous.map(|_| pairing), registry);
            shutdown::run_until(ep, shutdown::signal(), work).await?;
        }
        Cmd::Pair { net, rendezvous, code, name, out } => {
            let cfg = net.config()?;
            let ep = net.bind().await?;
            let result = pair::join(&ep, &cfg, rendezvous, &code, &name).await;
            ep.close().await;
            match result {
                Ok(desktop) => {
                    std::fs::write(&out, desktop.to_string())?;
                    println!("{desktop}");
                }
                Err(e) => {
                    eprintln!("pairing failed: {e:#}");
                    std::process::exit(match e.downcast_ref::<pair::JoinError>() {
                        Some(pair::JoinError::WrongCode) => 3,
                        Some(pair::JoinError::NoSuchCode) => 4,
                        None => 1,
                    });
                }
            }
        }
        Cmd::Rendezvous { net } => {
            let ep = net.bind().await?;
            tracing::info!("rendezvous id={}", ep.id());
            shutdown::run_until(ep.clone(), shutdown::signal(), rendezvous::serve(ep)).await?;
        }
        Cmd::Connect { net, peer, listen, status_json } => {
            let cfg = net.config()?;
            let ep = net.bind().await?;
            tracing::info!("connecting id={} -> peer={peer}", ep.id());
            let report = move |s: connect::Status| {
                if status_json {
                    let line = match s {
                        connect::Status::Connecting => serde_json::json!({"state": "connecting"}),
                        connect::Status::Connected(p) => serde_json::json!({"state": "connected", "path": p.to_string()}),
                        connect::Status::Revoked => serde_json::json!({"state": "revoked"}),
                    };
                    use std::io::Write;
                    let mut out = std::io::stdout().lock();
                    let _ = writeln!(out, "{line}");
                    let _ = out.flush();
                }
            };
            let work = connect::run_with_status(ep.clone(), endpoint::peer_addr(&cfg, peer), listen, report);
            if let Err(e) = shutdown::run_until(ep, shutdown::signal(), work).await {
                if e.downcast_ref::<connect::Revoked>().is_some() {
                    eprintln!("{e}");
                    std::process::exit(5);
                }
                return Err(e);
            }
        }
    }
    Ok(())
}
