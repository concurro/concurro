//! JSON tracing output shared by Rust services.

use tracing_subscriber::{EnvFilter, fmt};

/// Install a JSON subscriber that writes service spans to stdout.
pub fn init(default_filter: &str) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let filter =
        EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new(default_filter));
    fmt()
        .json()
        .with_env_filter(filter)
        .with_writer(std::io::stdout)
        .try_init()
}
