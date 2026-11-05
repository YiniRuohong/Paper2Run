//! Pairing over SPAKE2. Code = "NNN-NNNNN": a rendezvous nameplate plus a
//! 5-digit secret that only the two devices ever see. One attempt per code:
//! a wrong secret voids it, so a guesser gets a single 1-in-100000 try.
//!
//! On one ALPN_PAIR bi-stream (tablet T opens, desktop D answers):
//!   T->D  SPAKE2 msg A (len-prefixed)
//!   D->T  SPAKE2 msg B (len-prefixed)
//!   T->D  device name (len-prefixed), HMAC(K, "t"|T|D|name)
//!   D->T  HMAC(K, "d"|T|D|name)            -- or close(3) on mismatch
//! SPAKE2 identities are the TLS-authenticated endpoint ids.

use std::{
    fmt,
    path::PathBuf,
    str::FromStr,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::Context;
use hmac::{Hmac, Mac};
use iroh::{
    endpoint::{Connection, RecvStream, SendStream},
    Endpoint, EndpointId,
};
use sha2::Sha256;
use spake2::{Ed25519Group, Identity, Password, Spake2};
use tokio::sync::Mutex;

use crate::{devices, endpoint::{peer_addr, NetConfig}, rendezvous};
pub use crate::endpoint::ALPN_PAIR;

pub const CODE_TTL: Duration = Duration::from_secs(300);
const MAX_NAME: usize = 64;
const MAX_FRAME: usize = 256;
/// Connection close codes the tablet maps to [`JoinError`].
const CLOSE_WRONG_CODE: u32 = 3;
const CLOSE_NO_SESSION: u32 = 4;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Code {
    pub nameplate: u16,
    pub secret: u32,
}

impl fmt::Display for Code {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:03}-{:05}", self.nameplate, self.secret)
    }
}

impl FromStr for Code {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> anyhow::Result<Self> {
        let digits: String = s.chars().filter(|c| !matches!(c, '-' | ' ')).collect();
        anyhow::ensure!(digits.len() == 8 && digits.bytes().all(|b| b.is_ascii_digit()), "pairing code must be 8 digits, like 123-45678");
        Ok(Code { nameplate: digits[..3].parse()?, secret: digits[3..].parse()? })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PairState {
    None,
    Waiting { code: Code, expires_in: Duration },
    Paired { device: devices::Device },
    Expired,
    WrongCode,
}

#[derive(Debug, PartialEq, Eq)]
pub enum JoinError {
    WrongCode,
    NoSuchCode,
}

impl fmt::Display for JoinError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            JoinError::WrongCode => "wrong pairing code; the code is now void",
            JoinError::NoSuchCode => "pairing code not found or expired",
        })
    }
}
impl std::error::Error for JoinError {}

enum Phase {
    Waiting,
    Busy,
    Done(PairState),
}

struct Session {
    code: Code,
    desktop: EndpointId,
    expires: Instant,
    phase: Phase,
    _claim: rendezvous::Claim,
}

struct Inner {
    devices: PathBuf,
    ttl: Duration,
    session: Mutex<Option<Session>>,
}

/// Desktop-side pairing state, shared between the control API and the accept loop.
#[derive(Clone)]
pub struct Pairing(Arc<Inner>);

impl Pairing {
    pub fn new(devices: PathBuf) -> Self {
        Self::with_ttl(devices, CODE_TTL)
    }

    pub fn with_ttl(devices: PathBuf, ttl: Duration) -> Self {
        Pairing(Arc::new(Inner { devices, ttl, session: Mutex::new(None) }))
    }

    /// Claim a nameplate and open a new pairing session, voiding any previous one.
    pub async fn start(&self, ep: &Endpoint, cfg: &NetConfig, rendezvous: EndpointId) -> anyhow::Result<Code> {
        let claim = rendezvous::claim(ep, peer_addr(cfg, rendezvous)).await.context("rendezvous unreachable")?;
        let secret = {
            use rand::Rng;
            rand::rng().random_range(0..100_000)
        };
        let code = Code { nameplate: claim.nameplate, secret };
        *self.0.session.lock().await = Some(Session { code, desktop: ep.id(), expires: Instant::now() + self.0.ttl, phase: Phase::Waiting, _claim: claim });
        Ok(code)
    }

    pub async fn state(&self) -> PairState {
        let mut guard = self.0.session.lock().await;
        let Some(s) = guard.as_mut() else { return PairState::None };
        expire(s);
        match &s.phase {
            Phase::Waiting | Phase::Busy => PairState::Waiting { code: s.code, expires_in: s.expires.saturating_duration_since(Instant::now()) },
            Phase::Done(st) => st.clone(),
        }
    }

    /// Handle one incoming connection on ALPN_PAIR.
    pub async fn handle(&self, conn: Connection) {
        // Take the single attempt this code allows.
        let secret = {
            let mut guard = self.0.session.lock().await;
            match guard.as_mut() {
                Some(s) => {
                    expire(s);
                    if matches!(s.phase, Phase::Waiting) {
                        s.phase = Phase::Busy;
                        Some((s.code.secret, s.desktop))
                    } else {
                        None
                    }
                }
                None => None,
            }
        };
        let Some((secret, desktop)) = secret else {
            return conn.close(CLOSE_NO_SESSION.into(), b"no pairing session");
        };
        let outcome = self.host_exchange(&conn, secret, desktop).await;
        let paired = outcome.is_ok();
        let state = match outcome {
            Ok(device) => {
                tracing::info!("paired device id={} name={:?}", device.id, device.name);
                PairState::Paired { device }
            }
            Err(e) => {
                tracing::warn!("pairing attempt failed: {e:#}");
                conn.close(CLOSE_WRONG_CODE.into(), b"wrong code");
                PairState::WrongCode
            }
        };
        if let Some(s) = self.0.session.lock().await.as_mut() {
            s.phase = Phase::Done(state);
        }
        if paired {
            // Keep the connection until the tablet has read the final frame.
            let _ = tokio::time::timeout(Duration::from_secs(5), conn.closed()).await;
        }
    }

    async fn host_exchange(&self, conn: &Connection, secret: u32, desktop: EndpointId) -> anyhow::Result<devices::Device> {
        let tablet = conn.remote_id();
        let (mut send, mut recv) = conn.accept_bi().await?;
        let msg_a = read_frame(&mut recv).await?;
        let (spake, msg_b) = Spake2::<Ed25519Group>::start_b(&password(secret), &Identity::new(tablet.as_bytes()), &Identity::new(desktop.as_bytes()));
        write_frame(&mut send, &msg_b).await?;
        let key = spake.finish(&msg_a).map_err(|e| anyhow::anyhow!("spake2: {e:?}"))?;
        let name_raw = read_frame(&mut recv).await?;
        let tag_t = read_frame(&mut recv).await?;
        let name = String::from_utf8(name_raw).context("device name not utf-8")?;
        anyhow::ensure!(!name.is_empty() && name.len() <= MAX_NAME, "bad device name");
        confirm(&key, b"t", tablet, desktop, &name).verify_slice(&tag_t).map_err(|_| anyhow::anyhow!("key confirmation failed"))?;
        let device = devices::Device { id: tablet, name: name.clone(), paired_at: devices::now_unix() };
        devices::add(&self.0.devices, device.clone())?;
        let tag_d = confirm(&key, b"d", tablet, desktop, &name).finalize().into_bytes();
        write_frame(&mut send, &tag_d).await?;
        send.finish()?;
        Ok(device)
    }
}

fn expire(s: &mut Session) {
    if matches!(s.phase, Phase::Waiting) && Instant::now() >= s.expires {
        s.phase = Phase::Done(PairState::Expired);
    }
}

fn password(secret: u32) -> Password {
    Password::new(format!("p2r-pair-v1:{secret:05}").as_bytes())
}

fn confirm(key: &[u8], role: &[u8], tablet: EndpointId, desktop: EndpointId, name: &str) -> Hmac<Sha256> {
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(key).expect("any key length");
    for part in [b"p2r-pair-v1".as_slice(), role, tablet.as_bytes(), desktop.as_bytes(), name.as_bytes()] {
        mac.update(&(part.len() as u32).to_be_bytes());
        mac.update(part);
    }
    mac
}

async fn write_frame(send: &mut SendStream, data: &[u8]) -> anyhow::Result<()> {
    send.write_all(&(data.len() as u16).to_be_bytes()).await?;
    send.write_all(data).await?;
    Ok(())
}

async fn read_frame(recv: &mut RecvStream) -> anyhow::Result<Vec<u8>> {
    let mut len = [0u8; 2];
    recv.read_exact(&mut len).await?;
    let len = u16::from_be_bytes(len) as usize;
    anyhow::ensure!(len <= MAX_FRAME, "frame too large");
    let mut buf = vec![0u8; len];
    recv.read_exact(&mut buf).await?;
    Ok(buf)
}

/// Tablet side: returns the desktop's id on success.
pub async fn join(ep: &Endpoint, cfg: &NetConfig, rendezvous: EndpointId, code: &Code, device_name: &str) -> anyhow::Result<EndpointId> {
    anyhow::ensure!(!device_name.is_empty() && device_name.len() <= MAX_NAME, "device name must be 1-{MAX_NAME} bytes");
    let desktop = rendezvous::lookup(ep, peer_addr(cfg, rendezvous), code.nameplate)
        .await
        .map_err(|e| anyhow::Error::new(JoinError::NoSuchCode).context(format!("{e:#}")))?;
    let conn = ep.connect(peer_addr(cfg, desktop), ALPN_PAIR).await?;
    let result = join_exchange(&conn, ep.id(), code.secret, desktop, device_name).await;
    if result.is_err() {
        if let Some(reason) = conn.close_reason() {
            if let Some(e) = map_close(&reason) {
                return Err(anyhow::Error::new(e));
            }
        }
        // The desktop may still be closing; give it a moment to tell us why.
        if let Ok(reason) = tokio::time::timeout(Duration::from_secs(3), conn.closed()).await {
            if let Some(e) = map_close(&reason) {
                return Err(anyhow::Error::new(e));
            }
        }
    }
    conn.close(0u32.into(), b"done");
    result.map(|_| desktop)
}

fn map_close(reason: &iroh::endpoint::ConnectionError) -> Option<JoinError> {
    if let iroh::endpoint::ConnectionError::ApplicationClosed(close) = reason {
        return match u64::from(close.error_code) {
            c if c == CLOSE_WRONG_CODE as u64 => Some(JoinError::WrongCode),
            c if c == CLOSE_NO_SESSION as u64 => Some(JoinError::NoSuchCode),
            _ => None,
        };
    }
    None
}

async fn join_exchange(conn: &Connection, tablet: EndpointId, secret: u32, desktop: EndpointId, name: &str) -> anyhow::Result<()> {
    let (mut send, mut recv) = conn.open_bi().await?;
    let (spake, msg_a) = Spake2::<Ed25519Group>::start_a(&password(secret), &Identity::new(tablet.as_bytes()), &Identity::new(desktop.as_bytes()));
    write_frame(&mut send, &msg_a).await?;
    let msg_b = read_frame(&mut recv).await?;
    let key = spake.finish(&msg_b).map_err(|e| anyhow::anyhow!("spake2: {e:?}"))?;
    write_frame(&mut send, name.as_bytes()).await?;
    write_frame(&mut send, &confirm(&key, b"t", tablet, desktop, name).finalize().into_bytes()).await?;
    let tag_d = read_frame(&mut recv).await?;
    confirm(&key, b"d", tablet, desktop, name).verify_slice(&tag_d).map_err(|_| anyhow::Error::new(JoinError::WrongCode))?;
    Ok(())
}
