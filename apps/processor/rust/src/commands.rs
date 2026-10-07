//! Command implementations, shared by the HTTP server and the CLI.
//!
//! Every command takes the JSON payload the Python client sends and returns the
//! value it expects in `ProcessorResult.data`. A `Result::Err` carries a
//! user-readable sentence; the transports decide how to surface it.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::media;

/// Sample rate used for the waveform scan. 8 kHz mono is plenty to draw peaks
/// and keeps the decode fast even for long sources (the Python envelope uses the
/// same rate).
const WAVEFORM_SAMPLE_RATE: u32 = 8000;
const WAVEFORM_MIN_BUCKETS: usize = 20;
const WAVEFORM_MAX_BUCKETS: usize = 4000;
const FRAME_SECONDS_CAP: u32 = 300;

/// `health` — used by the API's readiness probe and the container healthcheck.
pub fn health(_payload: &Value) -> Result<Value, String> {
    let (ffmpeg, ffprobe) = media::tooling_available();
    Ok(json!({
        "ok": true,
        "service": "clipforge-processor",
        "version": env!("CARGO_PKG_VERSION"),
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "commands": ["health", "probe", "waveform", "frames"],
    }))
}

/// `probe` — stream metadata, normalized to the shape `media/ffmpeg.py` returns.
pub fn probe(payload: &Value) -> Result<Value, String> {
    let path = required_str(payload, "path")?;
    ensure_readable(&path)?;

    let raw = media::ffprobe_json(&path)?;
    let normalized = media::normalize_probe(&raw);
    if normalized
        .get("duration")
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        <= 0.0
    {
        // An image or a truncated file: report it rather than inventing a length.
        return Err("this file reports no duration".to_string());
    }
    Ok(json!({ "ok": true, "path": path, "data": normalized }))
}

/// `waveform` — peak envelope for the timeline UI from decoded PCM.
pub fn waveform(payload: &Value) -> Result<Value, String> {
    let path = required_str(payload, "path")?;
    ensure_readable(&path)?;
    let buckets = payload
        .get("buckets")
        .and_then(Value::as_u64)
        .unwrap_or(400)
        .clamp(WAVEFORM_MIN_BUCKETS as u64, WAVEFORM_MAX_BUCKETS as u64) as usize;

    let args = vec![
        "-v".to_string(),
        "error".to_string(),
        "-nostdin".to_string(),
        "-i".to_string(),
        path.clone(),
        "-vn".to_string(),
        "-ac".to_string(),
        "1".to_string(),
        "-ar".to_string(),
        WAVEFORM_SAMPLE_RATE.to_string(),
        "-f".to_string(),
        "s16le".to_string(),
        "-acodec".to_string(),
        "pcm_s16le".to_string(),
        "-".to_string(),
    ];
    let raw = media::ffmpeg_bytes(&args)?;

    let peaks = peaks_from_pcm(&raw, buckets);
    Ok(json!({ "ok": true, "path": path, "buckets": buckets, "peaks": peaks }))
}

/// `frames` — evenly spaced JPEGs for thumbnails and framing analysis previews.
pub fn frames(payload: &Value) -> Result<Value, String> {
    let path = required_str(payload, "path")?;
    ensure_readable(&path)?;
    let output_dir = required_str(payload, "output_dir")?;
    let fps = payload
        .get("fps")
        .and_then(Value::as_f64)
        .unwrap_or(1.0)
        .clamp(0.05, 60.0);
    let width = payload
        .get("width")
        .and_then(Value::as_u64)
        .unwrap_or(320)
        .clamp(64, 4096);
    let max_frames = payload
        .get("max_frames")
        .and_then(Value::as_u64)
        .unwrap_or(FRAME_SECONDS_CAP as u64)
        .clamp(1, 5000);

    let directory = PathBuf::from(&output_dir);
    std::fs::create_dir_all(&directory)
        .map_err(|error| format!("could not create {output_dir}: {error}"))?;

    let pattern = directory.join("frame_%05d.jpg");
    let filter = format!("fps={fps},scale={width}:-2");
    let args = vec![
        "-v".to_string(),
        "error".to_string(),
        "-nostdin".to_string(),
        "-i".to_string(),
        path.clone(),
        "-vf".to_string(),
        filter,
        "-frames:v".to_string(),
        max_frames.to_string(),
        "-q:v".to_string(),
        "3".to_string(),
        "-y".to_string(),
        pattern.to_string_lossy().to_string(),
    ];
    media::run_to_files(&args)?;

    let mut files: Vec<PathBuf> = std::fs::read_dir(&directory)
        .map_err(|error| format!("could not list {output_dir}: {error}"))?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|entry| {
            entry
                .extension()
                .map(|ext| ext.eq_ignore_ascii_case("jpg"))
                .unwrap_or(false)
        })
        .collect();
    files.sort();

    let frames: Vec<Value> = files
        .iter()
        .enumerate()
        .map(|(index, file)| {
            let size = std::fs::metadata(file).map(|meta| meta.len()).unwrap_or(0);
            json!({
                "index": index,
                // The `fps` filter samples evenly, so frame N sits at N/fps.
                "time_seconds": round3(index as f64 / fps),
                "path": file.to_string_lossy(),
                "bytes": size,
            })
        })
        .collect();

    Ok(json!({
        "ok": true,
        "path": path,
        "count": frames.len(),
        "fps": fps,
        "width": width,
        "frames": frames,
    }))
}

/// Route a CLI command name to its implementation.
pub fn dispatch(command: &str, payload: &Value) -> Result<Value, String> {
    match command {
        "health" => health(payload),
        "probe" => probe(payload),
        "waveform" => waveform(payload),
        "frames" => frames(payload),
        other => Err(format!("unknown command: {other}")),
    }
}

// ------------------------------------------------------------------ helpers ---
fn required_str(payload: &Value, key: &str) -> Result<String, String> {
    payload
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("missing required field: {key}"))
}

fn ensure_readable(path: &str) -> Result<(), String> {
    if Path::new(path).is_file() {
        Ok(())
    } else {
        Err(format!("no readable file at {path}"))
    }
}

/// Bucket raw little-endian signed 16-bit PCM into `buckets` peak values.
fn peaks_from_pcm(raw: &[u8], buckets: usize) -> Vec<f64> {
    if raw.len() < 2 || buckets == 0 {
        return vec![0.0; buckets];
    }
    let samples: Vec<f64> = raw
        .chunks_exact(2)
        .map(|pair| {
            let value = i16::from_le_bytes([pair[0], pair[1]]) as f64;
            (value / 32768.0).abs()
        })
        .collect();

    let per_bucket = samples.len().div_ceil(buckets).max(1);
    let mut peaks: Vec<f64> = samples
        .chunks(per_bucket)
        .map(|chunk| chunk.iter().copied().fold(0.0, f64::max))
        .collect();
    // Rounding may leave a partial final bucket: pad or trim to an exact count so
    // the UI can index the array directly.
    peaks.resize(buckets, 0.0);
    peaks.truncate(buckets);
    peaks.iter().map(|peak| round4(*peak)).collect()
}

fn round3(value: f64) -> f64 {
    (value * 1000.0).round() / 1000.0
}

fn round4(value: f64) -> f64 {
    (value * 10_000.0).round() / 10_000.0
}
