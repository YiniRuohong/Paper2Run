//! Paired devices, persisted as JSON: [{"id","name","paired_at"}].
//! A missing file means no devices. Writes are atomic and mode 0600.

use std::path::Path;

use anyhow::Context;
use iroh::EndpointId;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Device {
    pub id: EndpointId,
    pub name: String,
    /// Unix seconds.
    pub paired_at: u64,
}

pub fn list(path: &Path) -> anyhow::Result<Vec<Device>> {
    match std::fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes).with_context(|| format!("parse {}", path.display())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(e) => Err(e).with_context(|| format!("read {}", path.display())),
    }
}

/// Add or replace (by id).
pub fn add(path: &Path, d: Device) -> anyhow::Result<()> {
    let mut all = list(path)?;
    all.retain(|x| x.id != d.id);
    all.push(d);
    save(path, &all)
}

pub fn remove(path: &Path, id: EndpointId) -> anyhow::Result<bool> {
    let mut all = list(path)?;
    let before = all.len();
    all.retain(|x| x.id != id);
    if all.len() == before {
        return Ok(false);
    }
    save(path, &all)?;
    Ok(true)
}

fn save(path: &Path, all: &[Device]) -> anyhow::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("json.tmp");
    crate::keys::write_private(&tmp, &serde_json::to_vec_pretty(all)?)?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

pub fn now_unix() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}
