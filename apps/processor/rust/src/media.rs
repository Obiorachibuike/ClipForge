//! Thin wrappers around the FFmpeg/FFprobe binaries.
//!
//! The sidecar deliberately shells out to the system FFmpeg instead of linking
//! `libav*`: the container already ships a static FFmpeg, this keeps the build
//! dependency-free, and it means the Rust service and the Python pipeline agree
//! on media handling down to the encoder version.
//!
//! All calls are blocking and are invoked from `spawn_blocking` tasks (see
//! `http.rs`), so a slow decode never stalls the event loop.

use std::process::{Command, Stdio};

use serde_json::{json, Value};

/// Resolved FFmpeg binary; `PROCESSOR_FFMPEG` overrides the PATH lookup.
pub fn ffmpeg_bin() -> String {
    std::env::var("PROCESSOR_FFMPEG").unwrap_or_else(|_| "ffmpeg".to_string())
}

/// Resolved FFprobe binary; `PROCESSOR_FFPROBE` overrides the PATH lookup.
pub fn ffprobe_bin() -> String {
    std::env::var("PROCESSOR_FFPROBE").unwrap_or_else(|_| "ffprobe".to_string())
}

/// Whether the media binaries this service needs are actually present.
pub fn tooling_available() -> (bool, bool) {
    (which(&ffmpeg_bin()), which(&ffprobe_bin()))
}

fn which(binary: &str) -> bool {
    Command::new(binary)
        .arg("-version")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

/// Run FFmpeg and capture stdout bytes (used for raw PCM decoding).
pub fn ffmpeg_bytes(args: &[String]) -> Result<Vec<u8>, String> {
    let output = Command::new(ffmpeg_bin())
        .args(args)
        .stdin(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(|error| format!("could not execute ffmpeg: {error}"))?;

    if !output.status.success() {
        return Err(ffmpeg_error(&output.stderr, &output.status));
    }
    Ok(output.stdout)
}

/// Run a command that writes files (used by the frame extractor).
pub fn run_to_files(args: &[String]) -> Result<(), String> {
    let output = Command::new(ffmpeg_bin())
        .args(args)
        .stdin(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(|error| format!("could not execute ffmpeg: {error}"))?;

    if !output.status.success() {
        return Err(ffmpeg_error(&output.stderr, &output.status));
    }
    Ok(())
}

/// Turn FFmpeg's stderr into one actionable sentence, mirroring
/// `media/ffmpeg.py::_explain_stderr`.
fn ffmpeg_error(stderr: &[u8], status: &std::process::ExitStatus) -> String {
    let text = String::from_utf8_lossy(stderr);
    let mut lines: Vec<&str> = text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect();
    while let Some(line) = lines.pop() {
        if line.starts_with("Error")
            || line.starts_with("Invalid")
            || line.starts_with("Conversion failed")
            || line.starts_with("No such")
            || line.starts_with("Option")
        {
            return line.chars().take(400).collect();
        }
    }
    match status.code() {
        Some(code) => format!("ffmpeg exited with code {code}"),
        None => "ffmpeg was terminated by a signal".to_string(),
    }
}

/// Run FFprobe and parse its JSON description of the file.
pub fn ffprobe_json(path: &str) -> Result<Value, String> {
    let output = Command::new(ffprobe_bin())
        .args([
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ])
        .stdin(Stdio::null())
        .output()
        .map_err(|error| format!("could not execute ffprobe: {error}"))?;

    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr).trim().chars().take(400).collect::<String>();
        return Err(if detail.is_empty() {
            "ffprobe could not read this file".to_string()
        } else {
            detail
        });
    }
    serde_json::from_slice(&output.stdout).map_err(|error| format!("ffprobe returned malformed JSON: {error}"))
}

// ------------------------------------------------------------ field helpers ---
fn text_field(value: &Value, key: &str) -> String {
    value
        .get(key)
        .map(|entry| match entry {
            Value::String(text) => text.clone(),
            Value::Null => String::new(),
            other => other.to_string(),
        })
        .unwrap_or_default()
}

fn number_field(value: &Value, key: &str) -> f64 {
    value.get(key).and_then(Value::as_f64).unwrap_or(0.0)
}

fn integer_field(value: &Value, key: &str) -> i64 {
    match value.get(key) {
        Some(Value::Number(number)) => number.as_i64().unwrap_or(0),
        Some(Value::String(text)) => text.parse::<f64>().map(|parsed| parsed as i64).unwrap_or(0),
        _ => 0,
    }
}

/// Parse an FFprobe rational such as `30000/1001` into a frames-per-second value.
fn parse_rational(raw: &str) -> f64 {
    if raw.is_empty() {
        return 0.0;
    }
    match raw.split_once('/') {
        Some((numerator, denominator)) => {
            let top = numerator.trim().parse::<f64>().unwrap_or(0.0);
            let bottom = denominator.trim().parse::<f64>().unwrap_or(0.0);
            if bottom == 0.0 {
                0.0
            } else {
                top / bottom
            }
        }
        None => raw.trim().parse::<f64>().unwrap_or(0.0),
    }
}

/// Rotation is reported either as `side_data_list[].rotation` or the legacy
/// `tags.rotate`, depending on the container.
fn rotation_of(stream: &Value) -> i64 {
    if let Some(side_data) = stream.get("side_data_list").and_then(Value::as_array) {
        for entry in side_data {
            if let Some(rotation) = entry.get("rotation").and_then(Value::as_f64) {
                return rotation as i64;
            }
        }
    }
    integer_field(&stream.get("tags").cloned().unwrap_or(Value::Null), "rotate")
}

/// Convert an FFprobe document into the exact shape `media/ffmpeg.py` produces,
/// so callers cannot tell which implementation answered.
pub fn normalize_probe(raw: &Value) -> Value {
    let format = raw.get("format").cloned().unwrap_or(Value::Null);
    let streams: Vec<Value> = raw
        .get("streams")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let mut width = 0i64;
    let mut height = 0i64;
    let mut fps = 0.0f64;
    let mut video_codec = String::new();
    let mut audio_codec = String::new();
    let mut audio_channels = 0i64;
    let mut audio_sample_rate = 0i64;
    let mut has_audio = false;
    let mut rotation = 0i64;
    let mut nb_frames = 0i64;

    for stream in &streams {
        match text_field(stream, "codec_type").as_str() {
            "video" if video_codec.is_empty() => {
                video_codec = text_field(stream, "codec_name");
                width = integer_field(stream, "width");
                height = integer_field(stream, "height");
                fps = parse_rational(&text_field(stream, "avg_frame_rate"));
                if fps == 0.0 {
                    fps = parse_rational(&text_field(stream, "r_frame_rate"));
                }
                rotation = rotation_of(stream);
                nb_frames = integer_field(stream, "nb_frames");
            }
            "audio" if !has_audio => {
                has_audio = true;
                audio_codec = text_field(stream, "codec_name");
                audio_channels = integer_field(stream, "channels");
                audio_sample_rate = text_field(stream, "sample_rate").trim().parse::<i64>().unwrap_or(0);
            }
            _ => {}
        }
    }

    // `format.duration` is authoritative for containers that report it; a
    // stream-level duration is the fallback.
    let mut duration = text_field(&format, "duration").trim().parse::<f64>().unwrap_or(0.0);
    if duration <= 0.0 {
        for stream in &streams {
            let candidate = number_field(stream, "duration");
            if candidate > duration {
                duration = candidate;
            }
        }
    }

    json!({
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "audio_channels": audio_channels,
        "audio_sample_rate": audio_sample_rate,
        "has_audio": has_audio,
        "rotation": rotation,
        "container": text_field(&format, "format_name"),
        "bitrate": text_field(&format, "bit_rate").trim().parse::<i64>().unwrap_or(0),
        "nb_frames": nb_frames,
        "streams": streams,
    })
}
