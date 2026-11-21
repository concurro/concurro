//! Boundary-time credential eligibility from the Vault's nonsecret metadata.

use std::collections::BTreeMap;

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Utc};
use concurro_db::Database;
use concurro_exchange_v1::TurnSubmission;
use concurro_github_v1::response_status;
use concurro_tasks_core::{
    AttemptSpec, FundingMode, InitializationSnapshot, Provider, TaskState, design_roster,
    pr_review_roster, python_digest,
};
use concurro_tasks_postgres::{
    excluded_credential_versions, funding_slot, label_enrollment_request, refuse_stage_preflight,
};
use concurro_vault_v1::Metadata;
use reqwest::StatusCode;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{InitializationContext, InitializationTarget, intake_payload::assigned_agent, label};

/// A definitive personal-funding refusal that can block the task before charge.
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub(crate) struct FundingPreflightRefusal(pub &'static str);

/// Bind a paid turn or visibly block a definitive funding refusal.
pub(crate) async fn fund_or_block_attempt(
    database: &Database,
    context: &InitializationContext,
    state: &TaskState,
    snapshot: &InitializationSnapshot,
    target: &InitializationTarget,
    call: u64,
    attempt: &mut AttemptSpec,
) -> Result<bool> {
    let funded = async {
        if let Some(enrollment) = label_enrollment_request(database, state.id.0).await? {
            if !label::current_label_source(
                &context.github,
                &target.issue_repository,
                &target.issue_permissions,
                &enrollment,
            )
            .await?
            {
                return Err(FundingPreflightRefusal(
                    "autonomous label enrollment is no longer current",
                )
                .into());
            }
            let current_login = context
                .github
                .public_user_login_by_id(
                    &target.upstream,
                    &target.upstream_permissions,
                    enrollment.actor.id,
                )
                .await?
                .ok_or(FundingPreflightRefusal("labeler identity no longer exists"))?;
            if !context
                .github
                .writer_permission(
                    &target.upstream,
                    &current_login,
                    &target.upstream_permissions,
                )
                .await?
            {
                return Err(FundingPreflightRefusal(
                    "labeler no longer has repository write permission",
                )
                .into());
            }
        }
        fund_attempt(
            database,
            context,
            state.id.0,
            &snapshot.configuration,
            target,
            call,
            attempt,
        )
        .await
    }
    .await;
    match funded {
        Ok(()) => Ok(true),
        Err(error) => {
            let Some(refusal) = error.downcast_ref::<FundingPreflightRefusal>() else {
                return Err(error);
            };
            let mut work = database.begin().await?;
            let blocked = refuse_stage_preflight(
                &mut work,
                &context.outbox,
                state.id.0,
                state.revision,
                refusal.0,
            )
            .await?;
            ensure!(blocked, "funding preflight task revision changed");
            work.commit().await?;
            Ok(false)
        }
    }
}

/// Resolve list-era routes or the configured route retained for legacy records.
fn credential_routes(
    config: &Value,
    metadata: &Metadata,
    provider: Provider,
    profile: &str,
) -> Result<Vec<String>> {
    let provider_name = provider.as_str();
    let all_routes: &[&str] = match provider {
        Provider::Openai => &["openai-api-key", "chatgpt-subscription"],
        Provider::Anthropic => &["anthropic-api-key", "claude-subscription"],
    };
    let defaults = config
        .get("provider_credential_routes")
        .map(|value| {
            value
                .as_object()
                .context("provider credential routes are invalid")
        })
        .transpose()?;
    let overrides = config
        .get("credential_routes")
        .map(|value| value.as_object().context("credential routes are invalid"))
        .transpose()?;
    let configured = overrides
        .and_then(|routes| routes.get(profile))
        .or_else(|| defaults.and_then(|routes| routes.get(provider_name)))
        .map(|value| value.as_str().context("credential route is invalid"))
        .transpose()?;
    ensure!(
        configured.is_none_or(|route| all_routes.contains(&route)),
        "credential route does not support its provider"
    );
    if metadata
        .lists
        .get(provider_name)
        .is_some_and(|list| list.revision > 0)
    {
        Ok(all_routes.iter().map(|route| (*route).to_owned()).collect())
    } else {
        Ok(configured.into_iter().map(str::to_owned).collect())
    }
}

/// Resolve selected roles and return each provider's boundary-time fill eligibility.
pub(crate) fn boundary_eligibility(
    config: &Value,
    metadata: &Metadata,
    target: &str,
    at: DateTime<Utc>,
) -> Result<BTreeMap<Provider, Option<bool>>> {
    let mut available = BTreeMap::from([
        (Provider::Openai, Some(false)),
        (Provider::Anthropic, Some(false)),
    ]);
    if !metadata.history_complete {
        return Ok(available
            .into_keys()
            .map(|provider| (provider, None))
            .collect());
    }
    let mut requirements = Vec::new();
    for role in ["triage", "planner", "implementer"] {
        let (harness, _, _, agent) = assigned_agent(config, role)?;
        let profile = agent
            .as_ref()
            .and_then(|agent| agent["profile"].as_str())
            .unwrap_or(&harness);
        let provider = if harness == "codex" {
            Provider::Openai
        } else {
            Provider::Anthropic
        };
        requirements.push((role.to_owned(), profile.to_owned(), provider));
    }
    for (role, roster) in [
        ("plan_reviewers", design_roster(config)),
        ("pr_reviewers", pr_review_roster(config)),
    ] {
        for member in roster.map_err(anyhow::Error::msg)? {
            let provider_name = member
                .agent
                .as_ref()
                .and_then(|agent| agent["provider"].as_str())
                .unwrap_or(match member.provider.as_str() {
                    "codex" => "openai",
                    "claude" => "anthropic",
                    _ => "invalid",
                });
            let provider = match provider_name {
                "openai" => Provider::Openai,
                "anthropic" => Provider::Anthropic,
                _ => anyhow::bail!("reviewer provider is invalid"),
            };
            requirements.push((role.to_owned(), member.provider, provider));
        }
    }
    for (role, profile, provider) in requirements {
        let provider_name = provider.as_str();
        let routes = credential_routes(config, metadata, provider, &profile)?;
        if metadata
            .select(provider_name, &role, target, &profile, &routes, at)
            .is_some()
        {
            available.insert(provider, Some(true));
        }
    }
    Ok(available)
}

/// Bind one exact attempt to an acknowledged personal slot and current Vault version.
pub(crate) async fn fund_attempt(
    database: &Database,
    context: &InitializationContext,
    task_id: Uuid,
    configuration: &Value,
    target: &InitializationTarget,
    call: u64,
    attempt: &mut AttemptSpec,
) -> Result<()> {
    let mut submission: TurnSubmission = serde_json::from_slice(&attempt.submit_body)?;
    let request = &submission.payload["request"];
    let harness = request["provider"]
        .as_str()
        .context("attempt provider is missing")?;
    let provider = match harness {
        "codex" => Provider::Openai,
        "claude" => Provider::Anthropic,
        _ => anyhow::bail!("attempt provider is unsupported"),
    };
    let Some(funding) = funding_slot(database, task_id, provider).await? else {
        ensure!(
            configuration["funding_mode"] != "contributed",
            "task funding boundary is unavailable"
        );
        return Ok(());
    };
    if funding.mode == FundingMode::Org {
        return Ok(());
    }
    ensure!(
        funding.consent_current,
        "sponsorship source changed before admission"
    );
    ensure!(
        funding.acknowledged,
        "provider sponsorship transition is pending"
    );
    let slot = funding
        .slot
        .context("provider sponsorship slot is missing")?;
    if slot["status"] != "active" {
        return Err(FundingPreflightRefusal("provider has no active sponsor").into());
    }
    let provenance = slot["provenance"].as_str();
    if provenance != Some("explicit_comment")
        && !(matches!(provenance, Some("task_start" | "label" | "review_request"))
            && slot["source_funding_mode"] == "contributed")
    {
        return Err(
            FundingPreflightRefusal("source does not authorize contributed funding").into(),
        );
    }
    let sponsor = slot["sponsor_id"]
        .as_str()
        .context("sponsor identity is missing")?;
    let sponsor_id: i64 = sponsor.parse()?;
    let login = slot["login"].as_str().context("sponsor login is missing")?;
    let current_login = match context
        .github
        .public_user_id(&target.upstream, &target.upstream_permissions, login)
        .await
    {
        Ok(id) if id == sponsor_id => login.to_owned(),
        Ok(_) => context
            .github
            .public_user_login_by_id(&target.upstream, &target.upstream_permissions, sponsor_id)
            .await?
            .ok_or(FundingPreflightRefusal("sponsor identity no longer exists"))?,
        Err(error) if response_status(&error) == Some(StatusCode::NOT_FOUND) => context
            .github
            .public_user_login_by_id(&target.upstream, &target.upstream_permissions, sponsor_id)
            .await?
            .ok_or(FundingPreflightRefusal("sponsor identity no longer exists"))?,
        Err(error) => return Err(error),
    };
    if !context
        .github
        .writer_permission(
            &target.upstream,
            &current_login,
            &target.upstream_permissions,
        )
        .await?
    {
        return Err(
            FundingPreflightRefusal("sponsor no longer has repository write permission").into(),
        );
    }
    let vault = context
        .vault
        .as_ref()
        .context("contributed funding requires Vault metadata")?;
    let mut metadata = vault.read(sponsor_id, None).await?;
    let stage = request["stage"]
        .as_str()
        .context("attempt stage is missing")?;
    let role = match stage {
        "intake" => "triage",
        "plan" => "planner",
        "plan_qc" => "plan_reviewers",
        "develop" => "implementer",
        "review" => "pr_reviewers",
        _ => anyhow::bail!("attempt stage has no funding role"),
    };
    let profile = request["agent"]["profile"].as_str().unwrap_or(harness);
    let routes = credential_routes(configuration, &metadata, provider, profile)?;
    let model = request["model"]
        .as_str()
        .context("attempt model is missing")?;
    let now = Utc::now();
    let exclusions =
        excluded_credential_versions(database, task_id, sponsor_id, provider, model, now).await?;
    metadata.credentials.retain(|record| {
        !exclusions.iter().any(|(id, version)| {
            record.record_id == *id && i64::try_from(record.version).ok() == Some(*version)
        })
    });
    let record = metadata
        .select(
            provider.as_str(),
            role,
            &target.upstream,
            profile,
            &routes,
            now,
        )
        .ok_or(FundingPreflightRefusal(
            "no eligible personal credential for this role",
        ))?;
    ensure!(
        record.user_id == sponsor,
        "Vault credential owner differs from sponsor"
    );
    ensure!(
        record.record_id.len() == 64
            && record
                .record_id
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit()),
        "Vault credential record identity is invalid"
    );
    let credential_id = python_digest(&json!([
        "contributed",
        sponsor,
        record.route,
        record.record_id,
        record.version
    ]))?;
    let credential = json!({
        "id": credential_id, "provider": harness, "kind": "contributed",
        "adapter": "gateway-key", "account": sponsor,
        "route": record.route, "record_id": record.record_id,
        "version": record.version,
    });
    let generation = submission.binding["generation"]
        .as_i64()
        .context("task generation is missing")?;
    let admission = json!({
        "version": 1, "funding": "contributed", "credential": credential,
        "sponsorship_provider": provider.as_str(), "sponsor": sponsor,
        "epoch": slot["epoch"], "grant_id": slot["grant_id"],
        "route": record.route, "grant": slot,
        "call": call,
        "owner_generation": generation,
        "transcript_scope": request["session"]["scope"],
    });
    submission.payload["request"]["admission"] = admission;
    submission.payload["request"]["attempt"] = json!(attempt.id.to_string());
    let binding = submission
        .binding
        .as_object_mut()
        .context("attempt binding is invalid")?;
    binding.insert("version".to_owned(), json!(2));
    for (key, value) in [
        ("funding", json!("contributed")),
        ("sponsorship_provider", json!(provider.as_str())),
        ("sponsor", json!(sponsor)),
        (
            "epoch",
            submission.payload["request"]["admission"]["epoch"].clone(),
        ),
        (
            "grant_id",
            submission.payload["request"]["admission"]["grant_id"].clone(),
        ),
        ("route", json!(record.route)),
        ("credential", json!(record.record_id)),
        ("credential_version", json!(record.version)),
    ] {
        binding.insert(key.to_owned(), value);
    }
    let claims = submission
        .expected
        .as_object()
        .context("runner claims are invalid")?
        .iter()
        .filter(|(key, _)| key.as_str() != "ref")
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect::<serde_json::Map<_, _>>();
    attempt.staffing_group = python_digest(&json!([
        claims,
        submission.binding,
        submission.models,
        submission.budget,
        submission.lease as f64
    ]))?;
    attempt.submit_body = serde_json::to_vec(&submission)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::{
        collections::{BTreeMap, HashMap},
        process::Command,
    };

    use axum::{
        Json, Router,
        routing::{get, post},
    };
    use chrono::{DateTime, Duration, Utc};
    use concurro_contracts::TaskId;
    use concurro_db::Database;
    use concurro_events_postgres::{Outbox, migrate as migrate_events};
    use concurro_exchange_v1::TurnSubmission;
    use concurro_github_v1::AppClient;
    use concurro_tasks_core::{
        Actor, AttemptSpec, InitializationSnapshot, IssueKey, PinnedInputs, Provider, Request,
        RequestKind, Requirements, SourceOrder, Stage, TaskState,
    };
    use concurro_tasks_postgres::{baseline_page, create_baseline, migrate, record_request, task};
    use concurro_testkit::PostgresContainer;
    use concurro_vault_v1::Metadata;
    use serde_json::json;
    use sqlx::types::Json as SqlJson;
    use uuid::Uuid;

    use super::{boundary_eligibility, fund_or_block_attempt};
    use crate::{InitializationContext, InitializationTarget};

    #[test]
    fn listed_credential_fills_only_its_selected_provider_and_unknown_history_waits() {
        let config = json!({
            "planner":"codex", "implementer":"codex", "qc":"codex",
            "reviewers":["claude"], "models":{"codex":"main", "claude":"other"},
            "provider_credential_routes":{"openai":"chatgpt-subscription"}
        });
        let mut metadata: Metadata = serde_json::from_value(json!({
            "history_complete":true,
            "credentials":[{
                "record_id":"42:openai-api-key:1", "user_id":"42",
                "route":"openai-api-key", "version":1, "status":"valid",
                "models":null, "created":"2026-11-19T10:00:00Z",
                "updated":"2026-11-19T10:00:00Z", "kind":"api_key",
                "expiry_known":false, "expires_at":null,
                "entry_id":"entry-a", "provider":"openai", "name":"key",
                "enabled":true, "roles":["implementer"],
                "profiles":[{"target":"org/repo", "profile":"codex"}],
                "position":0, "removed":false
            }],
            "lists":{"openai":{"revision":3,"entries":["entry-a"]}}
        }))
        .expect("metadata");
        let at = DateTime::parse_from_rfc3339("2026-11-20T10:00:00Z")
            .expect("time")
            .to_utc();
        let available =
            boundary_eligibility(&config, &metadata, "org/repo", at).expect("eligibility");
        assert_eq!(available[&Provider::Openai], Some(true));
        assert_eq!(available[&Provider::Anthropic], Some(false));

        metadata.history_complete = false;
        let unknown =
            boundary_eligibility(&config, &metadata, "org/repo", at).expect("unknown history");
        assert_eq!(unknown[&Provider::Openai], None);
        assert_eq!(unknown[&Provider::Anthropic], None);

        metadata.history_complete = true;
        metadata.lists.clear();
        let route_era = boundary_eligibility(&config, &metadata, "org/repo", at)
            .expect("route-era eligibility");
        assert_eq!(route_era[&Provider::Openai], Some(false));
    }

    /// A removed autonomous label blocks an org-funded call before charge.
    #[tokio::test]
    async fn removed_autonomous_label_blocks_later_org_attempt_without_charge() {
        let postgres = PostgresContainer::start().await.expect("Postgres");
        let database = Database::connect(postgres.url(), 2)
            .await
            .expect("database");
        migrate(&database).await.expect("task schema");
        migrate_events(&database).await.expect("event schema");
        let app = Router::new()
            .route("/repos/acme/demo/installation", get(|| async {
                Json(json!({"id":7,"repository_selection":"selected","account":{"login":"acme"}}))
            }))
            .route("/app/installations/7/access_tokens", post(|Json(body): Json<serde_json::Value>| async move {
                Json(json!({"token":"scoped-token","permissions":body["permissions"],
                    "expires_at":(Utc::now()+Duration::minutes(55)).to_rfc3339()}))
            }))
            .route("/installation/repositories", get(|| async {
                Json(json!({"total_count":1,"repositories":[{"full_name":"acme/demo"}]}))
            }))
            .route("/repos/acme/demo/issues/3", get(|| async {
                Json(json!({"number":3,"state":"open","labels":[]}))
            }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let key = Command::new("openssl")
            .args([
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
            ])
            .output()
            .expect("test App key");
        assert!(key.status.success());
        let target = InitializationTarget {
            issue_repository: "acme/demo".to_owned(),
            fork: "acme/demo".to_owned(),
            upstream: "acme/demo".to_owned(),
            issue_permissions: BTreeMap::from([("issues".to_owned(), "read".to_owned())]),
            fork_permissions: BTreeMap::new(),
            upstream_permissions: BTreeMap::new(),
            bot_login: "concurro[bot]".to_owned(),
            publisher_logins: vec![],
            engine_sha: "c".repeat(40),
        };
        let context = InitializationContext {
            github: AppClient::new(
                reqwest::Client::new(),
                &format!("http://{address}"),
                42,
                &key.stdout,
            )
            .expect("App identity"),
            targets: HashMap::from([(8, target.clone())]),
            outbox: Outbox::new(1024 * 1024, vec!["task.>".to_owned()]),
            vault: None,
        };
        let issue = IssueKey {
            repository_id: 8,
            number: 3,
        };
        let actor = Actor {
            id: 7,
            login: "writer".to_owned(),
        };
        let enrolled = Request {
            id: Uuid::now_v7(),
            channel: "label".to_owned(),
            reply: "issue_label:101".to_owned(),
            comment_id: None,
            issue,
            issue_author: actor.clone(),
            actor: actor.clone(),
            kind: RequestKind::Start,
            order: SourceOrder {
                at: Utc::now() - Duration::hours(1),
                source_id: 101,
                source: "issue_label:101".to_owned(),
                index: 0,
            },
        };
        let mut work = database.begin().await.expect("record enrollment");
        record_request(&mut work, &enrolled)
            .await
            .expect("record label start");
        work.commit().await.expect("enrollment commit");
        let pins = PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 5,
            max_fix_rounds: 2,
            max_plan_revisions: 1,
        };
        let state = TaskState {
            id: TaskId(Uuid::now_v7()),
            issue,
            author: actor,
            stage: Stage::Plan,
            revision: 2,
            stage_reason: None,
            retry_stage: None,
            pins: Some(pins.clone()),
            used_model_calls: 1,
            current_attempt: None,
        };
        sqlx::query("INSERT INTO tasks.task_state (repository_id,issue_number,task_id,revision,state) VALUES (8,3,$1,2,$2)")
            .bind(state.id.0).bind(SqlJson(&state)).execute(database.pool()).await.expect("initialized task");
        sqlx::query("UPDATE tasks.requests SET status='applied' WHERE id=$1")
            .bind(enrolled.id)
            .execute(database.pool())
            .await
            .expect("applied enrollment");
        sqlx::query("INSERT INTO tasks.start_pins (task_id,request_id,task_revision,config_sha) VALUES ($1,$2,1,$3)")
            .bind(state.id.0).bind(enrolled.id).bind(&pins.config_sha)
            .execute(database.pool()).await.expect("pinned label source");
        let baseline = create_baseline(&database).await.expect("baseline");
        let snapshot = baseline_page(&database, baseline, 0, 100)
            .await
            .expect("baseline page")
            .expect("snapshot");
        let task_row = snapshot
            .iter()
            .find(|row| row.entity == "contribution_task")
            .expect("task row");
        assert_eq!(task_row.body["snapshot"]["mode"], "autonomous");
        assert_eq!(task_row.body["snapshot"]["enrollment"]["channel"], "label");
        let snapshot = InitializationSnapshot {
            pins: pins.clone(),
            configuration: json!({"repository":"acme/demo","funding_mode":"org","autonomous":true}),
            requirements: Requirements {
                title: "Task".to_owned(),
                body: None,
                discussion: vec![],
            },
            charter: "{}".to_owned(),
            instruction_texts: BTreeMap::new(),
            policy_documents: BTreeMap::new(),
            base_files: vec![],
            bundle_id: "bundle".to_owned(),
        };
        let turn = Uuid::now_v7();
        let submission = TurnSubmission {
            turn,
            expected: json!({}),
            models: vec!["gpt-test".to_owned()],
            budget: 1,
            lease: 1,
            payload: json!({"request":{"provider":"codex","model":"gpt-test","stage":"plan"}}),
            pin: None,
            binding: json!({}),
        };
        let mut attempt = AttemptSpec {
            id: turn,
            task: state.id,
            expected_revision: state.revision,
            staffing_group: "planner".to_owned(),
            fork: target.fork.clone(),
            config_sha: pins.config_sha,
            submit_body: serde_json::to_vec(&submission).expect("submit body"),
            prompt_body: None,
            deadline: Utc::now() + Duration::hours(1),
            unbounded_running: false,
        };
        assert!(
            !fund_or_block_attempt(
                &database,
                &context,
                &state,
                &snapshot,
                &target,
                2,
                &mut attempt
            )
            .await
            .expect("label preflight")
        );
        let blocked = task(&database, issue)
            .await
            .expect("task")
            .expect("blocked task");
        assert_eq!(blocked.stage, Stage::Blocked);
        assert!(
            blocked
                .stage_reason
                .as_deref()
                .is_some_and(|reason| reason.contains("label enrollment"))
        );
        assert_eq!(blocked.used_model_calls, 1);
        let attempts: i64 =
            sqlx::query_scalar("SELECT count(*) FROM tasks.attempts WHERE task_id=$1")
                .bind(state.id.0)
                .fetch_one(database.pool())
                .await
                .expect("attempt count");
        let grants: i64 =
            sqlx::query_scalar("SELECT count(*) FROM tasks.funding_grants WHERE task_id=$1")
                .bind(state.id.0)
                .fetch_one(database.pool())
                .await
                .expect("grant count");
        assert_eq!((attempts, grants), (0, 0));
        server.abort();
    }
}
