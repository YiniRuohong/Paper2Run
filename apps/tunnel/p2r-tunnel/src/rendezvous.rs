//! Rendezvous: maps a short-lived 3-digit nameplate to the desktop's endpoint id.
//! Runs as an iroh endpoint on the relay host, reached through the relay itself.
//! It never sees the pairing secret, only the nameplate.
//!
//! Wire format: one bi-stream per request, one JSON line each way.
//!   claim:  {"op":"claim"}                  -> {"nameplate":N,"ttl_ms":T}
//!   lookup: {"op":"lookup","nameplate":N}   -> {"id":"<endpoint id>"} | {"error":"..."}
//! A nameplate is released on lookup, on expiry, or when the claiming
//! connection closes.

use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant, SystemTime},
};

use anyhow::Context;
use iroh::{endpoint::Connection, Endpoint, EndpointAddr, EndpointId};
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

pub use crate::endpoint::ALPN_RENDEZVOUS;

pub const DEFAULT_TTL: Duration = Duration::from_secs(300);
const MAX_LINE: usize = 1024;

pub struct Claim {
    pub nameplate: u16,
    pub expires_at: SystemTime,
    _conn: Connection,
}

#[derive(Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "lowercase")]
enum Request {
    Claim,
    Lookup { nameplate: u16 },
}

#[derive(Serialize, Deserialize, Default)]
struct Response {
    #[serde(skip_serializing_if = "Option::is_none")]
    nameplate: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    ttl_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

struct Entry {
    id: EndpointId,
    expires: Instant,
    generation: u64,
}

#[derive(Default)]
struct Table {
    entries: HashMap<u16, Entry>,
    next_generation: u64,
}

impl Table {
    fn prune(&mut self, now: Instant) {
        self.entries.retain(|_, e| e.expires > now);
    }
}

pub async fn serve(ep: Endpoint) -> anyhow::Result<()> {
    serve_with_ttl(ep, DEFAULT_TTL).await
}

pub async fn serve_with_ttl(ep: Endpoint, ttl: Duration) -> anyhow::Result<()> {
    let table = Arc::new(Mutex::new(Table::default()));
    while let Some(incoming) = ep.accept().await {
        let table = table.clone();
        tokio::spawn(async move {
            let Ok(conn) = incoming.await else { return };
            if conn.alpn() != ALPN_RENDEZVOUS {
                return conn.close(1u32.into(), b"wrong protocol");
            }
            let peer = conn.remote_id();
            let mut mine = Vec::new();
            while let Ok((mut send, mut recv)) = conn.accept_bi().await {
                let Ok(line) = recv.read_to_end(MAX_LINE).await else { break };
                let resp = match serde_json::from_slice::<Request>(&line) {
                    Ok(Request::Claim) => {
                        let mut t = table.lock().await;
                        t.prune(Instant::now());
                        match free_nameplate(&t) {
                            Some(np) => {
                                t.next_generation += 1;
                                let generation = t.next_generation;
                                t.entries.insert(np, Entry { id: peer, expires: Instant::now() + ttl, generation });
                                mine.push((np, generation));
                                Response { nameplate: Some(np), ttl_ms: Some(ttl.as_millis() as u64), ..Default::default() }
                            }
                            None => Response { error: Some("no free nameplate".into()), ..Default::default() },
                        }
                    }
                    Ok(Request::Lookup { nameplate }) => {
                        let mut t = table.lock().await;
                        t.prune(Instant::now());
                        match t.entries.remove(&nameplate) {
                            Some(e) => Response { id: Some(e.id.to_string()), ..Default::default() },
                            None => Response { error: Some("unknown nameplate".into()), ..Default::default() },
                        }
                    }
                    Err(_) => Response { error: Some("bad request".into()), ..Default::default() },
                };
                let body = serde_json::to_vec(&resp).expect("serializable");
                let _ = send.write_all(&body).await;
                let _ = send.finish();
            }
            // Claimer gone: release whatever it still holds.
            let mut t = table.lock().await;
            for (np, generation) in mine {
                if t.entries.get(&np).is_some_and(|e| e.generation == generation) {
                    t.entries.remove(&np);
                }
            }
        });
    }
    Ok(())
}

fn free_nameplate(t: &Table) -> Option<u16> {
    use rand::Rng;
    if t.entries.len() >= 1000 {
        return None;
    }
    let mut rng = rand::rng();
    loop {
        let np: u16 = rng.random_range(0..1000);
        if !t.entries.contains_key(&np) {
            return Some(np);
        }
    }
}

async fn request(conn: &Connection, req: &Request) -> anyhow::Result<Response> {
    let (mut send, mut recv) = conn.open_bi().await?;
    send.write_all(&serde_json::to_vec(req)?).await?;
    send.finish()?;
    let body = recv.read_to_end(MAX_LINE).await?;
    let resp: Response = serde_json::from_slice(&body).context("bad rendezvous response")?;
    if let Some(e) = resp.error {
        anyhow::bail!("rendezvous: {e}");
    }
    Ok(resp)
}

pub async fn claim(ep: &Endpoint, rv: EndpointAddr) -> anyhow::Result<Claim> {
    let conn = ep.connect(rv, ALPN_RENDEZVOUS).await?;
    let resp = request(&conn, &Request::Claim).await?;
    let nameplate = resp.nameplate.context("rendezvous returned no nameplate")?;
    let ttl = Duration::from_millis(resp.ttl_ms.unwrap_or(0));
    Ok(Claim { nameplate, expires_at: SystemTime::now() + ttl, _conn: conn })
}

pub async fn lookup(ep: &Endpoint, rv: EndpointAddr, nameplate: u16) -> anyhow::Result<EndpointId> {
    let conn = ep.connect(rv, ALPN_RENDEZVOUS).await?;
    let resp = request(&conn, &Request::Lookup { nameplate }).await;
    conn.close(0u32.into(), b"done");
    let id = resp?.id.context("rendezvous returned no id")?;
    Ok(id.parse()?)
}
