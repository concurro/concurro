//! Content-addressed turn artifacts retained with the attempt transaction.

use anyhow::Result;
use concurro_contracts::TurnArtifact;
use concurro_db::UnitOfWork;
use sha2::{Digest, Sha256};
use uuid::Uuid;

/// Store original artifact bytes and link their digest to one turn.
pub(crate) async fn record_turn_artifact(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    body: &[u8],
    media_type: &str,
) -> Result<TurnArtifact> {
    let sha256 = hex::encode(Sha256::digest(body));
    sqlx::query(
        "INSERT INTO tasks.turn_artifacts (sha256,body,media_type) VALUES ($1,$2,$3) \
         ON CONFLICT (sha256) DO NOTHING",
    )
    .bind(&sha256)
    .bind(body)
    .bind(media_type)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "INSERT INTO tasks.turn_artifact_links (turn_id,sha256) VALUES ($1,$2) \
         ON CONFLICT DO NOTHING",
    )
    .bind(turn)
    .bind(&sha256)
    .execute(uow.connection())
    .await?;
    Ok(TurnArtifact {
        sha256,
        bytes: u64::try_from(body.len())?,
        media_type: media_type.to_owned(),
    })
}
