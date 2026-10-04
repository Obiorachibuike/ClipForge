//! HTTP transport: `POST /v1/{command}` plus health endpoints.
//!
//! The JSON bodies match what `app/services/processor.py` sends, so enabling the
//! sidecar needs no client change. Command handling is CPU- and disk-bound, so
//! each request runs on the blocking pool.

use axum::Json;
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::Router;
use serde_json::{json, Value};
use tokio::net::TcpListener;
use tracing::info;

use crate::commands;

/// Bind `addr` (e.g. `0.0.0.0:8100`) and serve until shutdown.
pub async fn serve(addr: &str) -> anyhow::Result<()> {
    let app = Router::new()
        .route("/healthz", get(liveness))
        .route("/readyz", get(readiness))
        .route("/v1/health", post(health))
        .route("/v1/probe", post(probe))
        .route("/v1/waveform", post(waveform))
        .route("/v1/frames", post(frames));

    let listener = TcpListener::bind(addr).await?;
    info!(address = addr, "processor.listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

/// Process liveness: the event loop is answering.
async fn liveness() -> (StatusCode, Json<Value>) {
    (StatusCode::OK, Json(json!({ "status": "ok" })))
}

/// Readiness: FFmpeg must be usable, otherwise every media command would fail.
async fn readiness() -> (StatusCode, Json<Value>) {
    let (_, Json(body)) = run_blocking(|| commands::health(&json!({}))).await;
    let ffmpeg_ok = body.get("ffmpeg").and_then(Value::as_bool).unwrap_or(false);
    if ffmpeg_ok {
        (StatusCode::OK, Json(body))
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, Json(body))
    }
}

// ---------------------------------------------------------------- handlers ---
async fn health(Json(payload): Json<Value>) -> (StatusCode, Json<Value>) {
    run_blocking(move || commands::health(&payload)).await
}

async fn probe(Json(payload): Json<Value>) -> (StatusCode, Json<Value>) {
    run_blocking(move || commands::probe(&payload)).await
}

async fn waveform(Json(payload): Json<Value>) -> (StatusCode, Json<Value>) {
    run_blocking(move || commands::waveform(&payload)).await
}

async fn frames(Json(payload): Json<Value>) -> (StatusCode, Json<Value>) {
    run_blocking(move || commands::frames(&payload)).await
}

// ----------------------------------------------------------------- helpers ---
/// Execute blocking work off the event loop and map the outcome onto a response.
async fn run_blocking<F>(work: F) -> (StatusCode, Json<Value>)
where
    F: FnOnce() -> Result<Value, String> + Send + 'static,
{
    match tokio::task::spawn_blocking(work).await {
        Ok(Ok(value)) => (StatusCode::OK, Json(value)),
        // 422 matches how the API reports "the input cannot be processed".
        Ok(Err(detail)) => (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({ "ok": false, "error": detail }))),
        Err(join_error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "ok": false, "error": format!("command panicked: {join_error}") })),
        ),
    }
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("processor.stopping");
}
