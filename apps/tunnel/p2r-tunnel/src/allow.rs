//! Allow-list of peer ids, one per line; `#` comments and blank lines ignored.
//! A missing file means nobody is allowed.

use std::{collections::HashSet, path::Path};

use anyhow::Context;
use iroh::EndpointId;

pub fn load(path: &Path) -> anyhow::Result<HashSet<EndpointId>> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(HashSet::new()),
        Err(e) => return Err(e).with_context(|| format!("read allow-list {}", path.display())),
    };
    text.lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .map(|l| l.parse::<EndpointId>().with_context(|| format!("bad peer id in allow-list: {l}")))
        .collect()
}
