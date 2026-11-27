//! Browser command validation, proof signing and task delivery.

use axum::{
    Json,
    extract::{Path, State, rejection::JsonRejection},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use concurro_contracts::{
    CommandClaims, CommandContent, CommandSubmission, RequestActor, RequestCommand, TaskIssue,
};
use jsonwebtoken::{Algorithm, EncodingKey, Header};
use reqwest::Client;
use serde::Deserialize;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{
    AccountState,
    browser::{SESSION, csrf},
    github::AccessError,
    sessions::Session,
};

const UNKNOWN: &str = "the command result is unknown; retry the same action ID";
const UNSENT: &str = "Concurro is unavailable; nothing was recorded";

/// Private signer and task request transport owned by the accounts process.
#[derive(Clone)]
pub struct Commands {
    client: Client,
    endpoint: String,
    key: std::sync::Arc<EncodingKey>,
    proof_seconds: i64,
}

impl Commands {
    /// Require a usable RSA key before accepting browser traffic.
    pub fn new(client: Client, task_api_url: String, pem: &[u8]) -> anyhow::Result<Self> {
        let key = EncodingKey::from_rsa_pem(pem)?;
        let endpoint = format!("{}/v1/requests", task_api_url.trim_end_matches('/'));
        reqwest::Url::parse(&endpoint)?;
        Ok(Self {
            client,
            endpoint,
            key: std::sync::Arc::new(key),
            proof_seconds: 180,
        })
    }

    /// Set a short lifetime for deterministic proof-expiry integration tests.
    pub fn with_proof_seconds(mut self, seconds: i64) -> Self {
        self.proof_seconds = seconds;
        self
    }

    /// Sign exact contents for one actor and action with a fresh 180-second expiry.
    pub fn sign(
        &self,
        actor: RequestActor,
        action_id: Uuid,
        content: CommandContent,
    ) -> anyhow::Result<CommandSubmission> {
        let claims = CommandClaims {
            actor,
            action_id,
            content: content.clone(),
            exp: (chrono::Utc::now().timestamp() + self.proof_seconds) as u64,
        };
        let proof = jsonwebtoken::encode(&Header::new(Algorithm::RS256), &claims, &self.key)?;
        Ok(CommandSubmission { content, proof })
    }

    /// Return a recorded response or classify delivery uncertainty.
    async fn send(
        &self,
        action_id: Uuid,
        submission: &CommandSubmission,
    ) -> Result<Value, CommandError> {
        let response = self
            .client
            .post(&self.endpoint)
            .header("Idempotency-Key", action_id.to_string())
            .json(submission)
            .send()
            .await
            .map_err(|error| {
                if error.is_connect() {
                    CommandError(StatusCode::SERVICE_UNAVAILABLE, UNSENT.to_owned())
                } else {
                    CommandError(StatusCode::BAD_GATEWAY, UNKNOWN.to_owned())
                }
            })?;
        let status = response.status();
        let value: Value = response
            .json()
            .await
            .map_err(|_| CommandError(StatusCode::BAD_GATEWAY, UNKNOWN.to_owned()))?;
        if status == StatusCode::ACCEPTED {
            if value.get("request_id").and_then(Value::as_str).is_some()
                && value.get("status").and_then(Value::as_str).is_some()
            {
                return Ok(value);
            }
            return Err(CommandError(StatusCode::BAD_GATEWAY, UNKNOWN.to_owned()));
        }
        let detail = value
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("the command failed");
        Err(CommandError(status, detail.to_owned()))
    }
}

/// Browser command body using the existing frontend shape.
#[derive(Deserialize)]
pub struct CommandBody {
    /// Command name.
    command: String,
    /// Optional browser argument.
    argument: Option<String>,
    /// Optional browser feedback attached to the proof.
    feedback: Option<String>,
}

/// Public browser command error with a safe detail.
pub struct CommandError(StatusCode, String);

impl IntoResponse for CommandError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"detail": self.1}))).into_response()
    }
}

/// Map one validated browser command to the existing typed request grammar.
fn typed_command(body: &CommandBody) -> Result<RequestCommand, CommandError> {
    let invalid = || {
        CommandError(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid command argument".to_owned(),
        )
    };
    let argument = body.argument.as_deref();
    match body.command.as_str() {
        "funding" => {
            let (provider, mode) = argument
                .and_then(|value| value.split_once(' '))
                .ok_or_else(invalid)?;
            if !["openai", "anthropic", "google"].contains(&provider)
                || !["org", "personal"].contains(&mode)
            {
                return Err(invalid());
            }
            Ok(RequestCommand::Funding {
                provider: provider.to_owned(),
                mode: mode.to_owned(),
            })
        }
        "sponsor" | "withdraw" => {
            if argument.is_some_and(|value| !["openai", "anthropic", "google"].contains(&value))
                || (body.command == "withdraw" && argument.is_none())
            {
                return Err(invalid());
            }
            if body.command == "sponsor" {
                Ok(RequestCommand::Sponsor {
                    provider: argument.map(str::to_owned),
                })
            } else {
                Ok(RequestCommand::Withdraw {
                    provider: argument.unwrap().to_owned(),
                })
            }
        }
        "approve" => Ok(RequestCommand::Approve {
            plan_id: argument
                .filter(|value| !value.is_empty())
                .ok_or_else(invalid)?
                .to_owned(),
        }),
        "retry" => match argument {
            Some(value) => Ok(RequestCommand::Extend {
                plan_revisions: value.parse().map_err(|_| invalid())?,
            }),
            None => Ok(RequestCommand::Retry),
        },
        "resume" if argument.is_none() => Ok(RequestCommand::Resume {
            description_only: false,
        }),
        "resume" => Err(invalid()),
        "start" | "stop" | "invalidate" if argument.is_none() => Ok(match body.command.as_str() {
            "start" => RequestCommand::Start,
            "stop" => RequestCommand::Stop,
            _ => RequestCommand::Invalidate,
        }),
        "start" | "stop" | "invalidate" => Err(invalid()),
        _ => Err(CommandError(
            StatusCode::UNPROCESSABLE_ENTITY,
            "unknown command".to_owned(),
        )),
    }
}

/// Admit one browser action after session, CSRF and per-kind GitHub authority checks.
pub async fn command(
    State(state): State<AccountState>,
    Path((owner, repo, number)): Path<(String, String, i64)>,
    headers: HeaderMap,
    jar: axum_extra::extract::CookieJar,
    body: Result<Json<CommandBody>, JsonRejection>,
) -> Result<Json<Value>, CommandError> {
    let session = state
        .sessions
        .get(jar.get(SESSION).map(|cookie| cookie.value()))
        .ok_or_else(|| CommandError(StatusCode::UNAUTHORIZED, "sign in with GitHub".to_owned()))?;
    if !csrf(&headers) {
        return Err(CommandError(
            StatusCode::FORBIDDEN,
            "missing request header".to_owned(),
        ));
    }
    let Json(body) = body.map_err(|_| {
        CommandError(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid command body".to_owned(),
        )
    })?;
    let typed = typed_command(&body)?;
    let action_id = headers
        .get("idempotency-key")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| Uuid::parse_str(value).ok())
        .ok_or_else(|| {
            CommandError(
                StatusCode::BAD_REQUEST,
                "missing or invalid action ID".to_owned(),
            )
        })?;
    let repository = format!("{owner}/{repo}");
    let hidden = || {
        CommandError(
            StatusCode::NOT_FOUND,
            format!("{repository} is not a Concurro target visible to your account"),
        )
    };
    let target = state
        .targets
        .for_repository(&repository)
        .await
        .map_err(|_| {
            CommandError(
                StatusCode::SERVICE_UNAVAILABLE,
                "targets unavailable".to_owned(),
            )
        })?
        .ok_or_else(hidden)?;
    let allowed = if matches!(typed, RequestCommand::Withdraw { .. }) {
        state.github.target_visible(&session.token, &target).await
    } else {
        state
            .github
            .tier(&session.token, &session.user, &target, &repository, number)
            .await
            .map(|tier| tier == "writer")
    };
    if !allowed.map_err(|error| access_error(&state, &session, error))? {
        return Err(hidden());
    }
    let repository_id = state
        .github
        .repository_id(&session.token, &repository)
        .await
        .map_err(|error| match error {
            AccessError::Hidden => hidden(),
            other => access_error(&state, &session, other),
        })?;
    let wire = serde_json::to_value(&typed).map_err(|_| {
        CommandError(
            StatusCode::SERVICE_UNAVAILABLE,
            "command unavailable".to_owned(),
        )
    })?;
    let content = CommandContent {
        issue: TaskIssue {
            repository_id,
            number,
        },
        kind: wire["kind"].as_str().unwrap_or_default().to_owned(),
        argument: wire.get("argument").cloned().unwrap_or_else(|| json!({})),
        detail: body
            .feedback
            .map(|feedback| json!({"feedback": feedback}))
            .unwrap_or_else(|| json!({})),
        source_at: session.source_at(action_id),
    };
    let commands = state.commands.as_ref().ok_or_else(|| {
        CommandError(
            StatusCode::SERVICE_UNAVAILABLE,
            "command unavailable".to_owned(),
        )
    })?;
    let submission = commands
        .sign(
            RequestActor {
                id: session.user.id,
                login: session.user.login.clone(),
            },
            action_id,
            content,
        )
        .map_err(|_| {
            CommandError(
                StatusCode::SERVICE_UNAVAILABLE,
                "command unavailable".to_owned(),
            )
        })?;
    Ok(Json(commands.send(action_id, &submission).await?))
}

/// Invalidate a remotely revoked session; preserve hidden and unavailable outcomes.
fn access_error(state: &AccountState, session: &Session, error: AccessError) -> CommandError {
    match error {
        AccessError::Revoked => {
            state.sessions.end(Some(&session.id));
            CommandError(StatusCode::UNAUTHORIZED, "sign in with GitHub".to_owned())
        }
        AccessError::Hidden => CommandError(
            StatusCode::NOT_FOUND,
            "issue repository is unavailable".to_owned(),
        ),
        AccessError::Unavailable => CommandError(
            StatusCode::SERVICE_UNAVAILABLE,
            "GitHub unavailable".to_owned(),
        ),
    }
}
