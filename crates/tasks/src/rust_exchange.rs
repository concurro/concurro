//! Rust exchange org submission and task observation adapter.

use std::collections::HashMap;

use anyhow::{Context, Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use concurro_contracts::{
    BlobRef, Envelope, ExchangeTurnFinished, ExchangeTurnStatus, ExpectedClaims, Grant,
    GrantCredential, GrantLimits, InferenceUsage, ProviderRequests, RunnerEngine, TurnBinding,
    TurnObservation as RustObservation, TurnPayloadV2, TurnResultV2, TurnSubmissionV2, TurnUsage,
};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{Handler, Outcome};
use concurro_events_postgres::Outbox;
use concurro_exchange_client::{ClientError, OperatorClient};
use concurro_exchange_v1::{Runner, TurnObservation, TurnSubmission};
use concurro_tasks_core::python_digest;
use concurro_tasks_postgres::{
    EffectKind, acknowledge_retirement, admit_rust_revision, claim_effect, expire_attempt,
    freeze_org_submit, org_attempt, record_rust_finished, record_rust_observation,
    record_submit_failure, record_usage, settle_submit, settle_submit_refusal,
};
use object_store::{ObjectStoreExt, path::Path};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::{ArchiveContext, upload_archive};

/// Org mode operator and target-frozen route selection.
#[derive(Clone, Debug)]
pub struct RustExchangeContext {
    /// Authenticated Rust operator API.
    pub operator: OperatorClient,
    /// Source repository for the v2 engine checkout.
    pub engine_repository: String,
    /// Provider-to-route names for each issue repository ID.
    pub routes: HashMap<i64, HashMap<String, String>>,
    /// Existing archive object store shared with the exchange runner.
    pub archive: ArchiveContext,
}

/// Durable exchange event application through the existing task settlement path.
#[derive(Clone)]
pub struct RustExchangeEvents {
    /// Operator reads used for reconciliation after terminal facts.
    pub operator: OperatorClient,
    /// Existing task outbox for `turn.*` read-model events.
    pub outbox: Outbox,
    /// Shared content-addressed artifact store.
    pub archive: ArchiveContext,
}

/// Convert an admitted v1 task attempt to its immutable v2 org submission.
async fn submission(
    database: &Database,
    context: &RustExchangeContext,
    turn: Uuid,
    bytes: &[u8],
) -> Result<(Vec<u8>, Option<String>)> {
    if let Ok(body) = serde_json::from_slice::<TurnSubmissionV2>(bytes) {
        if let Some(reference) = &body.payload.retained {
            blob(&context.archive, reference, "tar.gz").await?;
        }
        return Ok((bytes.to_vec(), None));
    }
    let old: TurnSubmission = serde_json::from_slice(bytes)?;
    ensure!(old.turn == turn, "stored turn identity changed");
    let org = org_attempt(database, turn).await?;
    let provider = old.payload["request"]["provider"]
        .as_str()
        .context("runner provider missing")?;
    let route = org
        .route
        .or_else(|| {
            context
                .routes
                .get(&org.repository_id)
                .and_then(|routes| routes.get(provider))
                .cloned()
        })
        .context("org route missing for target provider")?;
    let mut binding = old.binding.clone();
    let engine_sha = binding["engine"]
        .as_str()
        .context("engine SHA missing")?
        .to_owned();
    binding["engine"] = serde_json::to_value(RunnerEngine {
        repository: context.engine_repository.clone(),
        sha: engine_sha,
    })?;
    let binding: TurnBinding = serde_json::from_value(binding)?;
    let expected = &old.expected;
    let claims = ExpectedClaims {
        repository_id: expected["repository_id"]
            .as_str()
            .context("repository claim missing")?
            .to_owned(),
        repository_owner_id: expected["repository_owner_id"]
            .as_str()
            .context("owner claim missing")?
            .to_owned(),
        job_workflow_ref: expected["job_workflow_ref"]
            .as_str()
            .context("workflow claim missing")?
            .to_owned(),
        actor_id: expected["actor_id"]
            .as_str()
            .context("actor claim missing")?
            .to_owned(),
        job_workflow_sha: expected["job_workflow_sha"].as_str().map(str::to_owned),
    };
    let retained = if let Some(encoded) = old.payload["retained"].as_str() {
        let bytes = STANDARD.decode(encoded)?;
        let reference = BlobRef {
            sha256: hex::encode(Sha256::digest(&bytes)),
            size: bytes.len() as u64,
        };
        upload_archive(database, &context.archive, &reference.sha256).await?;
        ensure!(
            blob(&context.archive, &reference, "tar.gz").await? == bytes,
            "retained object differs from frozen submit"
        );
        Some(reference)
    } else {
        None
    };
    let payload = TurnPayloadV2 {
        request: old.payload["request"].clone(),
        feedback: old.payload["feedback"].clone(),
        spec: old.payload["spec"].clone(),
        timeout: old.payload["timeout"]
            .as_f64()
            .context("turn timeout missing")?,
        retained,
    };
    let mut body = TurnSubmissionV2 {
        attempt_id: turn,
        task_id: org.task_id,
        grant: Grant {
            grant_id: turn.to_string(),
            purpose: "task_org".to_owned(),
            payer: None,
            credential: GrantCredential {
                route: route.clone(),
                entry_id: None,
                record_id: None,
                version: None,
                custody: None,
                relay_id: None,
            },
            models: old.models.clone(),
            limits: GrantLimits {
                tokens: old.budget,
                lease_seconds: old.lease,
            },
        },
        binding,
        staffing_group: String::new(),
        expected_claims: claims,
        models: old.models,
        budget: old.budget,
        lease_seconds: old.lease,
        payload,
    };
    body.staffing_group = python_digest(&json!([
        body.expected_claims,
        body.binding,
        body.models,
        body.budget,
        body.lease_seconds as f64,
    ]))?;
    let bytes = serde_json::to_vec(&body)?;
    Ok((bytes, Some(route)))
}

/// Convert a v2 status/result to the existing task-domain observation format.
pub fn task_observation(source: &RustObservation, result: Option<Value>) -> TurnObservation {
    TurnObservation {
        turn: source.turn_id,
        status: if source.status == "finished" {
            "done".to_owned()
        } else {
            source.status.clone()
        },
        created: source.created.0.timestamp_millis() as f64 / 1000.0,
        started: source
            .started
            .map(|stamp| stamp.0.timestamp_millis() as f64 / 1000.0),
        finished: source
            .finished
            .map(|stamp| stamp.0.timestamp_millis() as f64 / 1000.0),
        dispatch: source.dispatch.as_ref().map(|dispatch| dispatch.session_id),
        runner: source.session_id.map(|session| Runner {
            session,
            run_id: source.run_id.clone(),
        }),
        result,
        provider_failure: source
            .provider_failure
            .as_ref()
            .and_then(|value| serde_json::to_value(value).ok()),
        refusal: source.refusal.clone(),
        raw_body: None,
    }
}

/// Fetch a verified exchange blob from the task application's shared object store.
async fn blob(archive: &ArchiveContext, reference: &BlobRef, suffix: &str) -> Result<Vec<u8>> {
    let key = Path::parse(format!(
        "{}/sha256/{}.{}",
        archive.prefix.trim_matches('/'),
        reference.sha256,
        suffix
    ))?;
    let bytes = archive.store.get(&key).await?.bytes().await?;
    ensure!(
        bytes.len() as u64 == reference.size
            && hex::encode(Sha256::digest(&bytes)) == reference.sha256,
        "exchange blob digest changed"
    );
    Ok(bytes.to_vec())
}

/// Restore v1 task archive/publication projections from the runner's v2 blob references.
async fn result_value(events: &RustExchangeEvents, result: &TurnResultV2) -> Result<Value> {
    let mut value = serde_json::to_value(result)?;
    if let Some(reference) = &result.workspace {
        value["workspace"] =
            serde_json::from_slice(&blob(&events.archive, reference, "json").await?)?;
    }
    for name in ["transcript", "retained"] {
        let reference = if name == "transcript" {
            &result.transcript
        } else {
            &result.retained
        };
        if let Some(reference) = reference {
            value[name] =
                Value::String(STANDARD.encode(blob(&events.archive, reference, "tar.gz").await?));
        }
    }
    Ok(value)
}

impl RustExchangeEvents {
    /// Settle one observed terminal fact through the shared task archive path.
    async fn apply_terminal(
        &self,
        work: &mut UnitOfWork<'_>,
        source: &RustObservation,
        result: Option<&TurnResultV2>,
        requests: ProviderRequests,
        usage: &TurnUsage,
    ) -> Result<()> {
        if let Some(revision) = source.revision {
            admit_rust_revision(work, source.turn_id, revision).await?;
        }
        let result = match result {
            Some(value) => Some(result_value(self, value).await?),
            None => None,
        };
        record_rust_finished(
            work,
            &self.outbox,
            &task_observation(source, result),
            requests,
            usage,
        )
        .await?;
        Ok(())
    }

    /// Read one stale attempt from the operator after a consumer outage or lost fact.
    pub async fn reconcile_turn(&self, database: &Database, turn: Uuid) -> Result<()> {
        let source = self.operator.observe(turn).await?;
        let mut work = database.begin().await?;
        if matches!(source.status.as_str(), "finished" | "lost" | "cancelled") {
            if let (Some(requests), Some(usage)) = (source.provider_requests, source.usage.as_ref())
            {
                self.apply_terminal(&mut work, &source, source.result.as_ref(), requests, usage)
                    .await?;
            } else if source.provider_failure.is_some()
                && let Some(revision) = source.revision
                && admit_rust_revision(&mut work, turn, revision).await?
            {
                record_rust_observation(&mut work, &self.outbox, &task_observation(&source, None))
                    .await?;
            }
        } else if let Some(revision) = source.revision
            && admit_rust_revision(&mut work, turn, revision).await?
        {
            record_rust_observation(&mut work, &self.outbox, &task_observation(&source, None))
                .await?;
        }
        work.commit().await?;
        Ok(())
    }
}

impl Handler<ExchangeTurnStatus> for RustExchangeEvents {
    /// Apply only nonterminal snapshots; the settled fact owns terminal accounting.
    async fn handle(
        &self,
        work: &mut UnitOfWork<'_>,
        event: Envelope<ExchangeTurnStatus>,
    ) -> Outcome {
        let status = event.data;
        let Some(revision) = event.revision else {
            return Outcome::Retry("status revision missing".to_owned());
        };
        if matches!(status.status.as_str(), "finished" | "lost" | "cancelled")
            && status.provider_failure.is_none()
        {
            return Outcome::Done;
        }
        let observation = RustObservation {
            turn_id: status.turn_id,
            status: status.status,
            session_id: status.session_id,
            run_id: status.run_id,
            dispatch: status.dispatch,
            refusal: status.refusal,
            created: status.created,
            started: status.started,
            finished: status.finished,
            result: None,
            provider_failure: status.provider_failure,
            provider_requests: None,
            revision: Some(revision),
            usage: None,
        };
        let result = async {
            if admit_rust_revision(work, status.turn_id, revision).await? {
                record_rust_observation(work, &self.outbox, &task_observation(&observation, None))
                    .await?;
            }
            Ok::<(), anyhow::Error>(())
        }
        .await;
        match result {
            Ok(_) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

impl Handler<ExchangeTurnFinished> for RustExchangeEvents {
    /// Read blobs and settle a terminal turn once, even after duplicate delivery.
    async fn handle(
        &self,
        work: &mut UnitOfWork<'_>,
        event: Envelope<ExchangeTurnFinished>,
    ) -> Outcome {
        let fact = event.data;
        let source = match fact.observation {
            Some(source) => source,
            None => match self.operator.observe(fact.turn_id).await {
                Ok(source) => source,
                Err(error) => return Outcome::Retry(error.to_string()),
            },
        };
        match self
            .apply_terminal(
                work,
                &source,
                fact.result.as_ref(),
                fact.provider_requests,
                &fact.usage,
            )
            .await
        {
            Ok(_) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

impl Handler<InferenceUsage> for RustExchangeEvents {
    /// Insert one request's attributed usage, independent of delivery count.
    async fn handle(&self, work: &mut UnitOfWork<'_>, event: Envelope<InferenceUsage>) -> Outcome {
        match record_usage(work, event.id.0, &event.data).await {
            Ok(()) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

/// Send one frozen org effect and apply its durable acknowledgement.
pub async fn run_effect(
    database: &Database,
    context: &RustExchangeContext,
    outbox: &Outbox,
    effect_id: i64,
) -> Result<()> {
    let mut claim = database.begin().await?;
    let Some(job) = claim_effect(&mut claim, effect_id).await? else {
        claim.commit().await?;
        return Ok(());
    };
    claim.commit().await?;
    match job.kind {
        EffectKind::Submit => {
            if job.recovered_exhaustion {
                let mut work = database.begin().await?;
                record_submit_failure(&mut work, outbox, &job).await?;
                work.commit().await?;
                return Ok(());
            }
            let mut due = database.begin().await?;
            let expired = expire_attempt(&mut due, outbox, job.turn, 0).await?;
            due.commit().await?;
            if expired {
                return Ok(());
            }
            let original = job
                .submit_body
                .as_deref()
                .context("stored submit body missing")?;
            let (body, freeze) = submission(database, context, job.turn, original).await?;
            if let Some(route) = freeze {
                let mut work = database.begin().await?;
                freeze_org_submit(&mut work, job.turn, original, &body, &route).await?;
                work.commit().await?;
            }
            let response = match context.operator.submit_bytes(job.turn, &body).await {
                Ok(value) => value,
                Err(ClientError::Refused { status, code })
                    if (400..500).contains(&status) && !matches!(status, 408 | 429) =>
                {
                    let mut work = database.begin().await?;
                    settle_submit_refusal(&mut work, outbox, &job, status, &code).await?;
                    work.commit().await?;
                    return Ok(());
                }
                Err(error) => {
                    let mut work = database.begin().await?;
                    record_submit_failure(&mut work, outbox, &job).await?;
                    work.commit().await?;
                    tracing::warn!(%error, turn=%job.turn, "Rust exchange submit failed");
                    return Ok(());
                }
            };
            let adapted = task_observation(&response, None);
            let mut work = database.begin().await?;
            if settle_submit(
                &mut work,
                outbox,
                &job,
                serde_json::to_value(&adapted)?,
                None,
            )
            .await?
                && let Some(revision) = response.revision
                && admit_rust_revision(&mut work, job.turn, revision).await?
            {
                record_rust_observation(&mut work, outbox, &adapted).await?;
            }
            work.commit().await?;
        }
        EffectKind::Retire => {
            let retired = context.operator.cancel(job.turn).await?;
            let mut work = database.begin().await?;
            acknowledge_retirement(&mut work, &job, &retired.sessions).await?;
            work.commit().await?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::{
        collections::{BTreeMap, HashMap},
        fs,
        process::Command,
        sync::Arc,
        time::Duration as StdDuration,
    };

    use axum::{
        Json, Router,
        body::Bytes,
        http::HeaderMap,
        routing::{get, post},
    };
    use base64::{
        Engine,
        engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
    };
    use chrono::Utc;
    use concurro_contracts::{
        BlobRef, Envelope, ExchangeTurnFinished, ExchangeTurnStatus, InferenceUsage,
    };
    use concurro_db::Database;
    use concurro_events::Handler;
    use concurro_events_postgres::{Outbox, migrate as migrate_events};
    use concurro_exchange::{
        AdmissionState, OperatorState, RunnerState, admission_router, operator_router,
        runner_router,
    };
    use concurro_exchange_postgres::{Store, migrate as migrate_exchange};
    use concurro_inference_gateway::{GatewayState, OrgRoute, router as gateway_router};
    use concurro_secrets::{SecretRef, Secrets};
    use concurro_tasks_core::{
        Actor, InitializationSnapshot, IssueKey, PinnedInputs, Requirements, Stage, TaskState,
    };
    use concurro_tasks_postgres::{
        due_rust_observations, freeze_org_submit, migrate as migrate_tasks, org_attempt,
        record_usage, runner_session,
    };
    use concurro_testkit::PostgresContainer;
    use jsonwebtoken::{Algorithm, EncodingKey, Header};
    use object_store::{ObjectStoreExt, memory::InMemory, path::Path};
    use rand::thread_rng;
    use rsa::{RsaPrivateKey, pkcs1::EncodeRsaPrivateKey, traits::PublicKeyParts};
    use serde_json::{Value, json};
    use sha2::{Digest, Sha256};
    use sqlx::types::Json as SqlJson;
    use tokio::sync::Mutex;
    use uuid::Uuid;

    use super::{RustExchangeContext, RustExchangeEvents, result_value, run_effect, submission};
    use crate::ArchiveContext;

    /// Freeze a task, execute its v2 engine through the org gateway, and ingest the result.
    #[tokio::test]
    async fn org_task_submit_and_terminal_fact_use_existing_read_model() {
        let postgres = PostgresContainer::start().await.expect("Postgres");
        let database = Database::connect(postgres.url(), 8)
            .await
            .expect("database");
        migrate_events(&database).await.expect("events");
        migrate_tasks(&database).await.expect("tasks");
        migrate_exchange(&database).await.expect("exchange");
        let task_outbox = Outbox::new(1_048_576, vec!["task.>".to_owned(), "turn.>".to_owned()]);
        let exchange = Store::new(
            database.clone(),
            Outbox::new(
                1_048_576,
                vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
            ),
        );
        let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let helper = repo.join("engine/tests/s4b_canned.py");
        let root = std::env::temp_dir().join(format!("concurro-s4c-engine-{}", Uuid::now_v7()));
        let env_path = format!(
            "{}:{}",
            repo.join("engine").display(),
            repo.join("engine/tests").display()
        );
        let prepared = Command::new("python3")
            .arg(&helper)
            .arg("prepare")
            .arg(&root)
            .env("PYTHONPATH", &env_path)
            .current_dir(&repo)
            .output()
            .expect("prepare source");
        assert!(
            prepared.status.success(),
            "source fixture: {}",
            String::from_utf8_lossy(&prepared.stderr)
        );
        let base_sha = String::from_utf8(prepared.stdout)
            .expect("base SHA")
            .trim()
            .to_owned();
        let retained = fs::read(root.join("retained-input.tar.gz")).expect("retained input");
        let retained_sha = hex::encode(Sha256::digest(&retained));
        let objects = Arc::new(InMemory::new());
        objects
            .put(
                &Path::parse(format!("blobs/sha256/{retained_sha}.tar.gz")).expect("blob key"),
                retained.clone().into(),
            )
            .await
            .expect("retained blob");
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("operator listener");
        let url = format!("http://{}", listener.local_addr().expect("address"));
        let service = tokio::spawn({
            let store = exchange.clone();
            async move {
                axum::serve(
                    listener,
                    operator_router(OperatorState {
                        store,
                        bearer: "operator-test".to_owned(),
                    }),
                )
                .await
                .expect("operator service");
            }
        });
        let task = Uuid::now_v7();
        let turn = Uuid::now_v7();
        let state = TaskState {
            id: concurro_contracts::TaskId(task),
            issue: IssueKey {
                repository_id: 7,
                number: 42,
            },
            author: Actor {
                id: 7,
                login: "author".to_owned(),
            },
            stage: Stage::Intake,
            revision: 1,
            stage_reason: None,
            retry_stage: None,
            pins: None,
            used_model_calls: 1,
            current_attempt: Some(turn),
        };
        sqlx::query("INSERT INTO tasks.issue_controls(repository_id,issue_number) VALUES(7,42)")
            .execute(database.pool())
            .await
            .expect("issue");
        sqlx::query("INSERT INTO tasks.task_state(repository_id,issue_number,task_id,revision,state) VALUES(7,42,$1,1,$2)")
            .bind(task).bind(SqlJson(&state)).execute(database.pool()).await.expect("task");
        let snapshot = InitializationSnapshot {
            pins: PinnedInputs {
                config_sha: "a".repeat(40),
                base_sha: base_sha.clone(),
                max_model_calls: 4,
                max_fix_rounds: 2,
                max_plan_revisions: 2,
            },
            configuration: json!({"repository":"acme/upstream"}),
            requirements: Requirements {
                title: "Fixture".to_owned(),
                body: Some("Do the fixture".to_owned()),
                discussion: vec![],
            },
            charter: json!({"goals":{"g1":"Complete the fixture"}}).to_string(),
            instruction_texts: BTreeMap::new(),
            policy_documents: BTreeMap::new(),
            base_files: vec!["docs/guide.md".to_owned()],
            bundle_id: "fixture".to_owned(),
        };
        sqlx::query("INSERT INTO tasks.initialization_inputs(task_id,task_revision,snapshot) VALUES($1,1,$2)")
            .bind(task).bind(SqlJson(&snapshot)).execute(database.pool()).await.expect("pinned inputs");
        let body = json!({
            "turn":turn,
            "expected":{"repository_id":"7","repository_owner_id":"8","job_workflow_ref":"acme/fork/.github/workflows/agent.yml@refs/tags/v2","actor_id":"9","event_name":"workflow_dispatch","ref":""},
            "models":["claude-test"],"budget":1000,"lease":3600,"pin":null,
            "binding":{"repository":"acme/upstream","issue":42,"role":"intake:claude",
                "engine":base_sha,"fork":"acme/fork","upstream":"acme/upstream",
                "config_sha":"config","base":base_sha,"phase":"single"},
            "payload":{"request":{"provider":"claude","model":"claude-test","stage":"intake",
                "gateway":{"url":"http://untrusted.invalid/v1"},
                "session":{"scope":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","turn":0},
                "context":{"base":base_sha}},
                "feedback":{},"spec":{"refs":{"main":base_sha},"head":"main","base_branch":"main","candidate":null},
                "timeout":30.0,"retained":STANDARD.encode(&retained)}
        });
        sqlx::query("INSERT INTO tasks.attempts(id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body,initial_deadline,deadline,unbounded_running,status) VALUES($1,$2,'intake',1,'old-group','acme/fork',$3,$4,now()+interval '1 hour',now()+interval '1 hour',false,'ready')")
            .bind(turn).bind(task).bind("a".repeat(40)).bind(serde_json::to_vec(&body).expect("body"))
            .execute(database.pool()).await.expect("attempt");
        sqlx::query(
            "INSERT INTO tasks.funding_grants(attempt_id,task_id,purpose) VALUES($1,$2,'task_org')",
        )
        .bind(turn)
        .bind(task)
        .execute(database.pool())
        .await
        .expect("grant");
        let effect: i64 = sqlx::query_scalar("INSERT INTO tasks.effects(turn_id,kind,status) VALUES($1,'submit','recorded') RETURNING id")
            .bind(turn).fetch_one(database.pool()).await.expect("effect");
        let operator = concurro_exchange_client::OperatorClient::new(
            reqwest::Client::new(),
            url,
            "operator-test",
        );
        let context = RustExchangeContext {
            operator: operator.clone(),
            engine_repository: "acme/engine".to_owned(),
            routes: HashMap::from([(
                7,
                HashMap::from([("claude".to_owned(), "claude_org".to_owned())]),
            )]),
            archive: ArchiveContext {
                store: objects.clone(),
                prefix: "blobs".to_owned(),
            },
        };
        let missing = RustExchangeContext {
            archive: ArchiveContext {
                store: Arc::new(InMemory::new()),
                prefix: "blobs".to_owned(),
            },
            ..context.clone()
        };
        assert!(
            submission(
                &database,
                &missing,
                turn,
                &serde_json::to_vec(&body).expect("body")
            )
            .await
            .is_err(),
            "unavailable retained bytes cannot be sent as a reference"
        );
        run_effect(&database, &context, &task_outbox, effect)
            .await
            .expect("Rust submit");
        let frozen: Vec<u8> =
            sqlx::query_scalar("SELECT submit_body FROM tasks.attempts WHERE id=$1")
                .bind(turn)
                .fetch_one(database.pool())
                .await
                .expect("frozen body");
        let (replayed, route) = submission(&database, &context, turn, &frozen)
            .await
            .expect("verified replay");
        assert_eq!(replayed, frozen);
        assert!(route.is_none());
        let frozen: concurro_contracts::TurnSubmissionV2 =
            serde_json::from_slice(&frozen).expect("v2 body");
        assert_eq!(frozen.grant.credential.route, "claude_org");
        assert_eq!(frozen.binding.engine.repository, "acme/engine");
        let session = operator
            .observe(turn)
            .await
            .expect("turn")
            .session_id
            .expect("session");
        assert!(
            runner_session(&database, session)
                .await
                .expect("runner session")
                .is_some()
        );
        let (private, jwks) = {
            let private = RsaPrivateKey::new(&mut thread_rng(), 2048).expect("RSA key");
            let public = private.to_public_key();
            let pem = private.to_pkcs1_pem(Default::default()).expect("PEM");
            let jwks = json!({"keys":[{"kty":"RSA","alg":"RS256","use":"sig","kid":"fake-github",
                "n":URL_SAFE_NO_PAD.encode(public.n().to_bytes_be()),
                "e":URL_SAFE_NO_PAD.encode(public.e().to_bytes_be())}]});
            (
                EncodingKey::from_rsa_pem(pem.as_bytes()).expect("signing key"),
                jwks,
            )
        };
        let jwks_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("JWKS listener");
        let jwks_url = format!(
            "http://{}/jwks",
            jwks_listener.local_addr().expect("JWKS address")
        );
        let jwks_server = tokio::spawn(async move {
            axum::serve(
                jwks_listener,
                Router::new().route("/jwks", get(move || async move { Json(jwks.clone()) })),
            )
            .await
            .expect("JWKS server");
        });
        let provider_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("provider listener");
        let upstream = format!(
            "http://{}/v1",
            provider_listener.local_addr().expect("provider address")
        );
        let provider_server = tokio::spawn(async move {
            axum::serve(provider_listener, Router::new().route("/v1/canned", post(|headers:HeaderMap, bytes:Bytes| async move {
                assert_eq!(headers.get("x-api-key").expect("org key"), "org-test-token");
                assert_eq!(serde_json::from_slice::<Value>(&bytes).expect("provider request")["model"], "claude-test");
                Json(json!({"type":"message","message":"fake inference","stop_reason":"end_turn","usage":{"input_tokens":13,"output_tokens":5}}))
            }))).await.expect("provider server");
        });
        let admission_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("admission listener");
        let admission_url = format!(
            "http://{}",
            admission_listener.local_addr().expect("admission address")
        );
        let admission_store = exchange.clone();
        let admission_server = tokio::spawn(async move {
            axum::serve(
                admission_listener,
                admission_router(AdmissionState {
                    store: admission_store,
                    bearer: "admission-test".to_owned(),
                }),
            )
            .await
            .expect("admission server");
        });
        let org_file = root.join("org-token");
        fs::write(&org_file, "org-test-token\n").expect("org token fixture");
        let gateway_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("gateway listener");
        let gateway_url = format!(
            "http://{}",
            gateway_listener.local_addr().expect("gateway address")
        );
        let gateway_server = tokio::spawn(async move {
            axum::serve(
                gateway_listener,
                gateway_router(GatewayState {
                    admission: concurro_exchange_client::AdmissionClient::new(
                        reqwest::Client::new(),
                        admission_url,
                        "admission-test",
                    ),
                    provider: reqwest::Client::new(),
                    routes: Arc::new(HashMap::from([(
                        "claude_org".to_owned(),
                        OrgRoute {
                            upstream,
                            token: SecretRef::File(org_file),
                            auth: "x-api-key".to_owned(),
                            account: None,
                            anthropic_beta: None,
                        },
                    )])),
                    secrets: Arc::new(Secrets::new(None, StdDuration::from_secs(60))),
                }),
            )
            .await
            .expect("gateway server");
        });
        let token_file = root.join("runner-token-key");
        fs::write(&token_file, "test-token-key-of-sufficient-length").expect("runner key fixture");
        let runner_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("runner listener");
        let runner_url = format!(
            "http://{}",
            runner_listener.local_addr().expect("runner address")
        );
        let runner_objects = objects.clone();
        let runner_store = exchange.clone();
        let runner_server = tokio::spawn(async move {
            axum::serve(
                runner_listener,
                runner_router(RunnerState {
                    store: runner_store,
                    secrets: Arc::new(Secrets::new(None, StdDuration::from_secs(60))),
                    token_key: SecretRef::File(token_file),
                    jwks_url,
                    inference_base_url: format!("{gateway_url}/v1"),
                    objects: runner_objects,
                    object_prefix: "blobs".to_owned(),
                    http: reqwest::Client::new(),
                    jwks: Arc::new(Mutex::new(None)),
                }),
            )
            .await
            .expect("runner server");
        });
        let now = Utc::now().timestamp();
        let mut header = Header::new(Algorithm::RS256);
        header.kid = Some("fake-github".to_owned());
        let oidc = jsonwebtoken::encode(
            &header,
            &json!({
                "iss":"https://token.actions.githubusercontent.com","aud":"concurro-gateway",
                "iat":now,"nbf":now,"exp":now+300,"jti":"s4c-org-fixture",
                "repository_id":"7","repository_owner_id":"8",
                "job_workflow_ref":"acme/fork/.github/workflows/agent.yml@refs/tags/v2",
                "actor_id":"9","event_name":"workflow_dispatch",
                "ref":format!("refs/heads/concurro/runs/{session}"),
                "runner_environment":"github-hosted","run_id":"900","run_attempt":"1",
            }),
            &private,
        )
        .expect("OIDC token");
        let output = tokio::task::spawn_blocking(move || {
            Command::new("python3")
                .arg(helper)
                .arg("serve")
                .arg(&root)
                .arg(&runner_url)
                .arg(session.to_string())
                .arg(oidc)
                .env("PYTHONPATH", env_path)
                .current_dir(repo)
                .output()
        })
        .await
        .expect("engine join")
        .expect("engine process");
        assert!(
            output.status.success(),
            "engine failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let finished: Envelope<ExchangeTurnFinished> =
            tokio::time::timeout(StdDuration::from_secs(5), async {
                loop {
                    let event: Option<Value> =
                        sqlx::query_scalar("SELECT message FROM events.outbox WHERE subject=$1")
                            .bind(format!("exchange.turn.{turn}.finished"))
                            .fetch_optional(database.pool())
                            .await
                            .expect("exchange fact query");
                    if let Some(event) = event {
                        break serde_json::from_value(event).expect("finished contract");
                    }
                    tokio::time::sleep(StdDuration::from_millis(30)).await;
                }
            })
            .await
            .expect("exchange finished fact");
        let archive = ArchiveContext {
            store: objects,
            prefix: "blobs".to_owned(),
        };
        let events = RustExchangeEvents {
            operator,
            outbox: task_outbox,
            archive,
        };
        let workspace = json!({"files":[{"path":"docs/guide.md","status":"modified"}]});
        let workspace_bytes = serde_json::to_vec(&workspace).expect("workspace JSON");
        let workspace_sha = hex::encode(Sha256::digest(&workspace_bytes));
        events
            .archive
            .store
            .put(
                &Path::parse(format!("blobs/sha256/{workspace_sha}.json")).expect("workspace key"),
                workspace_bytes.clone().into(),
            )
            .await
            .expect("workspace blob");
        let mut publication_result = finished.data.result.clone().expect("runner result");
        publication_result.workspace = Some(BlobRef {
            sha256: workspace_sha,
            size: workspace_bytes.len() as u64,
        });
        assert_eq!(
            result_value(&events, &publication_result)
                .await
                .expect("publication projection")["workspace"],
            workspace
        );
        sqlx::query(
            "UPDATE tasks.attempts SET rust_observed_at=now()-interval '6 minutes' WHERE id=$1",
        )
        .bind(turn)
        .execute(database.pool())
        .await
        .expect("stale observation");
        assert!(
            due_rust_observations(&database)
                .await
                .expect("reconcile scan")
                .contains(&turn)
        );
        assert_eq!(
            exchange
                .prune(Utc::now() + chrono::Duration::days(2))
                .await
                .expect("prune terminal"),
            1
        );
        assert!(
            events.operator.observe(turn).await.is_err(),
            "live operator row was pruned"
        );
        let mut work = database.begin().await.expect("apply fact");
        assert_eq!(
            events.handle(&mut work, finished.clone()).await,
            concurro_events::Outcome::Done
        );
        work.commit().await.expect("fact commit");
        let prior: Value = sqlx::query_scalar(
            "SELECT message FROM events.outbox WHERE subject=$1 ORDER BY id LIMIT 1",
        )
        .bind(format!("exchange.turn.{turn}.status"))
        .fetch_one(database.pool())
        .await
        .expect("prior snapshot");
        let prior: Envelope<ExchangeTurnStatus> =
            serde_json::from_value(prior).expect("status contract");
        let mut work = database.begin().await.expect("stale status");
        assert_eq!(
            events.handle(&mut work, prior).await,
            concurro_events::Outcome::Done
        );
        work.commit().await.expect("stale status commit");
        let status: String = sqlx::query_scalar("SELECT status FROM tasks.attempts WHERE id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("task attempt status");
        assert_eq!(status, "finished");
        let fact: Value = sqlx::query_scalar("SELECT message FROM events.outbox WHERE subject=$1")
            .bind(format!("turn.{turn}.finished"))
            .fetch_one(database.pool())
            .await
            .expect("task read-model fact");
        assert_eq!(fact["data"]["provider_requests"], "some");
        assert_eq!(fact["data"]["usage"]["requests"], 1);
        assert_eq!(fact["data"]["usage"]["input_tokens"], 13);
        assert!(fact["data"]["transcript"].is_object());
        // Intake does not produce a publication workspace; development does.
        assert!(fact["data"]["workspace"].is_null());
        let archived: Option<String> =
            sqlx::query_scalar("SELECT gzip_sha256 FROM tasks.turn_archives WHERE turn_id=$1")
                .bind(turn)
                .fetch_one(database.pool())
                .await
                .expect("retained archive row");
        let retained = finished
            .data
            .result
            .as_ref()
            .and_then(|result| result.retained.as_ref())
            .expect("runner retained archive");
        assert_eq!(archived.as_deref(), Some(retained.sha256.as_str()));
        let count: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE subject=$1")
            .bind(format!("turn.{turn}.finished"))
            .fetch_one(database.pool())
            .await
            .expect("one task fact");
        assert_eq!(count, 1);
        let usage: Value =
            sqlx::query_scalar("SELECT message FROM events.outbox WHERE subject='inference.usage'")
                .fetch_one(database.pool())
                .await
                .expect("exchange usage fact");
        let usage: Envelope<InferenceUsage> =
            serde_json::from_value(usage).expect("usage contract");
        let mut review_usage = usage.data.clone();
        let replay = usage.clone();
        let mut work = database.begin().await.expect("usage");
        assert_eq!(
            events.handle(&mut work, usage).await,
            concurro_events::Outcome::Done
        );
        work.commit().await.expect("usage commit");
        let mut work = database.begin().await.expect("usage replay");
        assert_eq!(
            events.handle(&mut work, replay).await,
            concurro_events::Outcome::Done
        );
        work.commit().await.expect("usage replay commit");
        let count: i64 =
            sqlx::query_scalar("SELECT count(*) FROM tasks.funding_usage WHERE turn_id=$1")
                .bind(turn)
                .fetch_one(database.pool())
                .await
                .expect("funding usage");
        assert_eq!(count, 1);
        let stage: String =
            sqlx::query_scalar("SELECT state->>'stage' FROM tasks.task_state WHERE task_id=$1")
                .bind(task)
                .fetch_one(database.pool())
                .await
                .expect("settled stage");
        assert_eq!(stage, "Plan");
        let review_task = Uuid::now_v7();
        let review_turn = Uuid::now_v7();
        sqlx::query(
            "INSERT INTO tasks.standalone_review_tasks \
             (id,repository_id,runner_repository_id,upstream,pr_number,reviewer_id,stage) \
             VALUES ($1,11,7,'acme/upstream',5,9,'review')",
        )
        .bind(review_task)
        .execute(database.pool())
        .await
        .expect("standalone review");
        let original = serde_json::to_vec(&body).expect("review body");
        sqlx::query(
            "INSERT INTO tasks.attempts \
             (id,standalone_task_id,operation,expected_revision,staffing_group,fork,config_sha, \
              submit_body,initial_deadline,deadline,unbounded_running,status) \
             VALUES ($1,$2,'review',1,'old-group','acme/fork',$3,$4, \
                     now()+interval '1 hour',now()+interval '1 hour',false,'sending')",
        )
        .bind(review_turn)
        .bind(review_task)
        .bind("a".repeat(40))
        .bind(&original)
        .execute(database.pool())
        .await
        .expect("review attempt");
        sqlx::query(
            "INSERT INTO tasks.standalone_review_funding_grants \
             (attempt_id,task_id,purpose) VALUES ($1,$2,'org')",
        )
        .bind(review_turn)
        .bind(review_task)
        .execute(database.pool())
        .await
        .expect("review grant");
        let selected = org_attempt(&database, review_turn)
            .await
            .expect("review route source");
        assert_eq!((selected.task_id, selected.repository_id), (review_task, 7));
        let mut work = database.begin().await.expect("freeze review");
        freeze_org_submit(
            &mut work,
            review_turn,
            &original,
            b"frozen-review",
            "claude_org",
        )
        .await
        .expect("freeze review route");
        work.commit().await.expect("review freeze commit");
        let route: String = sqlx::query_scalar(
            "SELECT route FROM tasks.standalone_review_funding_grants WHERE attempt_id=$1",
        )
        .bind(review_turn)
        .fetch_one(database.pool())
        .await
        .expect("frozen review route");
        assert_eq!(route, "claude_org");
        review_usage.request_id = Uuid::now_v7();
        review_usage.turn_id = review_turn;
        review_usage.grant_id = review_turn.to_string();
        let mut work = database.begin().await.expect("review usage");
        record_usage(&mut work, Uuid::now_v7(), &review_usage)
            .await
            .expect("review usage row");
        work.commit().await.expect("review usage commit");
        let counted: i64 =
            sqlx::query_scalar("SELECT count(*) FROM tasks.funding_usage WHERE turn_id=$1")
                .bind(review_turn)
                .fetch_one(database.pool())
                .await
                .expect("review usage count");
        assert_eq!(counted, 1);
        runner_server.abort();
        gateway_server.abort();
        admission_server.abort();
        provider_server.abort();
        jwks_server.abort();
        service.abort();
    }
}
