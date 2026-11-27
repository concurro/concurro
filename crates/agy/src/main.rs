//! Standard-input bridge from the retained Python runner to the Rust agy adapter.

use std::{env, path::Path, process::ExitCode};

use anyhow::{Context, Result};
use concurro_agy::runner::{self, Interrupted, Invocation};
use tokio::io::{self, AsyncReadExt};

/// Read one bound invocation and emit only the successful structured-result envelope.
async fn run() -> Result<()> {
    let mut bytes = Vec::new();
    io::stdin().read_to_end(&mut bytes).await?;
    let input: Invocation = serde_json::from_slice(&bytes)?;
    let key = env::var("CONCURRO_GATEWAY_KEY").context("derived gateway key missing")?;
    let completed = runner::invoke(input, Path::new("agy"), &key).await?;
    println!("{}", serde_json::to_string(&completed)?);
    Ok(())
}

/// Preserve the runner's distinct timeout exit status without printing provider content.
#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) if error.is::<Interrupted>() => {
            let interrupted = error
                .downcast_ref::<Interrupted>()
                .expect("checked interruption");
            eprintln!("{interrupted}");
            ExitCode::from(match interrupted {
                Interrupted::Timeout => 76,
                Interrupted::Signal => 75,
            })
        }
        Err(error) => {
            eprintln!("agy invocation failed: {error:#}");
            ExitCode::FAILURE
        }
    }
}
