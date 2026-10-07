//! ClipForge processor — optional Rust media-orchestration sidecar.
//!
//! Two modes, one JSON contract (see `README.md`):
//!
//! ```text
//! clipforge-processor serve          # HTTP service on 0.0.0.0:8100
//! clipforge-processor probe          # CLI: JSON payload on stdin, JSON on stdout
//! clipforge-processor waveform
//! clipforge-processor frames
//! clipforge-processor health
//! ```
//!
//! The sidecar is stateless and optional: the Python pipeline implements the
//! same commands in-process, and `app/services/processor.py` falls back to them
//! whenever this service is disabled or unreachable. Nothing in the web app may
//! depend on it being present.

mod commands;
mod http;
mod media;

use std::io::Read;
use std::process::ExitCode;

use serde_json::{json, Value};
use tracing::error;
use tracing_subscriber::EnvFilter;

const DEFAULT_ADDRESS: &str = "0.0.0.0:8100";

fn main() -> ExitCode {
    init_tracing();
    let args: Vec<String> = std::env::args().skip(1).collect();

    match args.first().map(String::as_str) {
        Some("serve") => {
            let address = args
                .get(1)
                .cloned()
                .or_else(|| std::env::var("PROCESSOR_BIND").ok())
                .unwrap_or_else(|| DEFAULT_ADDRESS.to_string());
            run_server(&address)
        }
        Some("health") | Some("probe") | Some("waveform") | Some("frames") => {
            let command = args[0].clone();
            ExitCode::from(run_cli(&command))
        }
        Some("--help") | Some("-h") => {
            print_usage();
            ExitCode::SUCCESS
        }
        Some(other) => {
            eprintln!("unknown command: {other}\n");
            print_usage();
            ExitCode::from(2)
        }
        None => {
            print_usage();
            ExitCode::SUCCESS
        }
    }
}

fn run_server(address: &str) -> ExitCode {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build();
    let runtime = match runtime {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("could not start the async runtime: {error}");
            return ExitCode::FAILURE;
        }
    };
    match runtime.block_on(http::serve(address)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            error!(error = %error, "processor.start_failed");
            eprintln!("could not serve on {address}: {error}");
            ExitCode::FAILURE
        }
    }
}

/// CLI mode: read one JSON object from stdin, write one JSON object to stdout.
/// A non-zero exit tells the Python client to use `stderr` as the detail message.
fn run_cli(command: &str) -> u8 {
    let mut input = String::new();
    if let Err(error) = std::io::stdin().read_to_string(&mut input) {
        eprintln!("could not read the request from stdin: {error}");
        return 1;
    }
    let payload: Value = if input.trim().is_empty() {
        json!({})
    } else {
        match serde_json::from_str(&input) {
            Ok(value) => value,
            Err(error) => {
                eprintln!("malformed JSON request: {error}");
                return 1;
            }
        }
    };

    match commands::dispatch(command, &payload) {
        Ok(value) => match serde_json::to_string(&value) {
            Ok(encoded) => {
                println!("{encoded}");
                0
            }
            Err(error) => {
                eprintln!("could not encode the response: {error}");
                1
            }
        },
        Err(detail) => {
            eprintln!("{detail}");
            1
        }
    }
}

fn print_usage() {
    println!(
        "clipforge-processor {version}\n\
         \n\
         Optional media orchestration sidecar for ClipForge.\n\
         \n\
         USAGE:\n    \
         clipforge-processor serve [ADDRESS]     HTTP service (default {DEFAULT_ADDRESS})\n    \
         clipforge-processor health              {\"ok\": true} when the service is usable\n    \
         clipforge-processor probe               {{\"path\": \"...\"}} -> stream metadata\n    \
         clipforge-processor waveform            {{\"path\": \"...\", \"buckets\": 400}} -> peaks\n    \
         clipforge-processor frames              {{\"path\": \"...\", \"output_dir\": \"...\"}}\n\
         \n\
         COMMANDS read a JSON object on stdin and write one to stdout.\n\
         ENV: PROCESSOR_BIND, PROCESSOR_FFMPEG, PROCESSOR_FFPROBE, RUST_LOG",
        version = env!("CARGO_PKG_VERSION"),
        DEFAULT_ADDRESS = DEFAULT_ADDRESS,
    );
}

fn init_tracing() {
    let filter = EnvFilter::try_from_env("RUST_LOG").unwrap_or_else(|_| EnvFilter::new("info"));
    let json_logs = std::env::var("LOG_JSON")
        .map(|value| value == "true")
        .unwrap_or(false);
    let builder = tracing_subscriber::fmt().with_env_filter(filter);
    // `try_init` keeps a double init (tests, embedding) from panicking.
    let _ = if json_logs {
        builder.json().try_init()
    } else {
        builder.try_init()
    };
}
