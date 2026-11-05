//! Desktop side: accept allow-listed peers and reverse-proxy each QUIC stream
//! as one HTTP/1.1 connection to the loopback backend.

use std::{convert::Infallible, path::PathBuf, sync::Arc};

use bytes::Bytes;
use http::{header, HeaderValue, Request, Response, StatusCode, Uri};
use http_body_util::{combinators::BoxBody, BodyExt, Full};
use hyper::body::Incoming;
use hyper_util::{
    client::legacy::{connect::HttpConnector, Client},
    rt::{TokioExecutor, TokioIo},
};
use iroh::{endpoint::Connection, Endpoint};

type Body = BoxBody<Bytes, hyper::Error>;
type Upstream = Client<HttpConnector, Incoming>;

pub async fn run(ep: Endpoint, backend_port: u16, allow_path: PathBuf) -> anyhow::Result<()> {
    let client: Upstream = Client::builder(TokioExecutor::new()).build_http();
    let ctx = Arc::new(Ctx { client, backend_port });
    while let Some(incoming) = ep.accept().await {
        let ctx = ctx.clone();
        let allow_path = allow_path.clone();
        tokio::spawn(async move {
            let conn = match incoming.await {
                Ok(c) => c,
                Err(e) => return tracing::debug!("handshake failed: {e}"),
            };
            let peer = conn.remote_id();
            match crate::allow::load(&allow_path) {
                Ok(set) if set.contains(&peer) => {}
                Ok(_) => {
                    tracing::warn!("peer={peer} rejected: not in allow-list");
                    return conn.close(1u32.into(), b"not allowed");
                }
                Err(e) => {
                    tracing::error!("allow-list unreadable, rejecting peer={peer}: {e:#}");
                    return conn.close(1u32.into(), b"not allowed");
                }
            }
            crate::paths::spawn_logger(conn.clone());
            serve_conn(conn, ctx).await;
        });
    }
    Ok(())
}

struct Ctx {
    client: Upstream,
    backend_port: u16,
}

async fn serve_conn(conn: Connection, ctx: Arc<Ctx>) {
    while let Ok((send, recv)) = conn.accept_bi().await {
        let ctx = ctx.clone();
        tokio::spawn(async move {
            let io = TokioIo::new(tokio::io::join(recv, send));
            let svc = hyper::service::service_fn(move |req| proxy(req, ctx.clone()));
            if let Err(e) = hyper::server::conn::http1::Builder::new().serve_connection(io, svc).await {
                tracing::debug!("stream ended: {e}");
            }
        });
    }
}

async fn proxy(mut req: Request<Incoming>, ctx: Arc<Ctx>) -> Result<Response<Body>, Infallible> {
    let authority = format!("127.0.0.1:{}", ctx.backend_port);
    let path = req.uri().path_and_query().map(|p| p.as_str()).unwrap_or("/");
    *req.uri_mut() = match format!("http://{authority}{path}").parse::<Uri>() {
        Ok(u) => u,
        Err(_) => return Ok(status(StatusCode::BAD_REQUEST)),
    };
    let headers = req.headers_mut();
    headers.insert(header::HOST, HeaderValue::from_str(&authority).expect("ascii"));
    if headers.contains_key(header::ORIGIN) {
        headers.insert(header::ORIGIN, HeaderValue::from_str(&format!("http://{authority}")).expect("ascii"));
    }
    headers.remove("sec-fetch-site");
    match ctx.client.request(req).await {
        Ok(resp) => Ok(resp.map(|b| b.boxed())),
        Err(e) => {
            tracing::warn!("backend unreachable: {e}");
            Ok(status(StatusCode::BAD_GATEWAY))
        }
    }
}

fn status(code: StatusCode) -> Response<Body> {
    let mut r = Response::new(Full::new(Bytes::from(code.canonical_reason().unwrap_or(""))).map_err(|e| match e {}).boxed());
    *r.status_mut() = code;
    r
}
