//! The allow-list is the set of paired device ids (see [`crate::devices`]).
//! A missing file means nobody is allowed; an unreadable one is an error.

use std::{collections::HashSet, path::Path};

use iroh::EndpointId;

pub fn load(path: &Path) -> anyhow::Result<HashSet<EndpointId>> {
    Ok(crate::devices::list(path)?.into_iter().map(|d| d.id).collect())
}
