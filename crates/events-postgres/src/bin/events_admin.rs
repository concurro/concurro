//! Operator commands for parked and held events in one service database.

use std::env;

use anyhow::{Context, Result};
use concurro_db::Database;
use concurro_events_postgres::run_admin;

/// Execute one operator command against the configured service database.
#[tokio::main]
async fn main() -> Result<()> {
    let url = env::var("DATABASE_URL").context("DATABASE_URL is required")?;
    let consumer = env::var("CONSUMER").context("CONSUMER is required")?;
    let database = Database::connect(&url, 2).await?;
    let args: Vec<_> = env::args().skip(1).collect();
    println!("{}", run_admin(&database, &consumer, &args).await?);
    Ok(())
}
