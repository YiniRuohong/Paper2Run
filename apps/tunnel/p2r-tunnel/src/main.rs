mod keys;

use std::path::PathBuf;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "p2r-tunnel", about = "Paper2Run tablet tunnel over iroh")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Print this device's public id (creating the key if needed).
    Id {
        #[arg(long)]
        key: PathBuf,
    },
}

fn main() -> anyhow::Result<()> {
    match Cli::parse().cmd {
        Cmd::Id { key } => {
            println!("{}", keys::load_or_create(&key)?.public());
        }
    }
    Ok(())
}
