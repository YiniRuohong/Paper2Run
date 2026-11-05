//! Persistent iroh identity: a raw 32-byte secret key stored with mode 0600.

use std::path::Path;

use anyhow::Context;

pub fn load_or_create(path: &Path) -> anyhow::Result<iroh::SecretKey> {
    if path.exists() {
        let bytes = std::fs::read(path).with_context(|| format!("read key {}", path.display()))?;
        let raw: [u8; 32] = bytes
            .as_slice()
            .try_into()
            .map_err(|_| anyhow::anyhow!("key file {} is not 32 bytes", path.display()))?;
        return Ok(iroh::SecretKey::from_bytes(&raw));
    }
    let key = iroh::SecretKey::generate();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("tmp");
    write_private(&tmp, &key.to_bytes())?;
    std::fs::rename(&tmp, path)?;
    Ok(key)
}

fn write_private(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    use std::io::Write;
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(path)?;
    f.write_all(bytes)?;
    f.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn creates_key_with_0600() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested").join("key");
        load_or_create(&path).unwrap();
        let mode = std::fs::metadata(&path).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }

    #[test]
    fn reload_returns_same_key() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("key");
        let a = load_or_create(&path).unwrap();
        let b = load_or_create(&path).unwrap();
        assert_eq!(a.public(), b.public());
    }

    #[test]
    fn rejects_corrupt_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("key");
        std::fs::write(&path, b"garbage").unwrap();
        assert!(load_or_create(&path).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), b"garbage");
    }
}
