//! S0 spike: GitHub Actions OIDC verification in Rust with `jsonwebtoken`, applying the
//! Python exchange's rules (`gateway/exchange/exchange.py:147, 906`).

use std::collections::{BTreeMap, HashSet};

use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode, decode_header, jwk::JwkSet};
use serde_json::Value;

pub const ISSUER: &str = "https://token.actions.githubusercontent.com";
pub const SKEW: i64 = 60;
pub const MAX_AGE: i64 = 600;

/// Why a token was refused, and whether the session records it.
#[derive(Debug, PartialEq)]
pub enum Refusal {
    /// 401, not recorded: malformed, bad signature, bad time claims or a reused `jti`.
    Unauthenticated(&'static str),
    /// 403, not recorded: another repository's token.
    ForeignRepository,
    /// 403, recorded on the session.
    Recorded(String),
}

/// One session's expectations and its bound run, if any.
pub struct Session {
    pub expected: BTreeMap<String, String>,
    pub ref_name: String,
    pub run: Option<String>,
}

/// Verify a token against the JWKS and the session; returns the claims and the run
/// (`run_id/run_attempt`) to bind. `seen` holds used `jti` values.
pub fn verify(
    token: &str,
    jwks: &JwkSet,
    audience: &str,
    session: &Session,
    seen: &mut HashSet<String>,
    now: i64,
) -> Result<(Value, String), Refusal> {
    let header = decode_header(token).map_err(|_| Refusal::Unauthenticated("malformed token"))?;
    if header.alg != Algorithm::RS256 {
        return Err(Refusal::Unauthenticated("invalid token signature"));
    }
    let jwk = header
        .kid
        .as_deref()
        .and_then(|kid| jwks.find(kid))
        .ok_or(Refusal::Unauthenticated("invalid token signature"))?;
    let key = DecodingKey::from_jwk(jwk).map_err(|_| Refusal::Unauthenticated("invalid token signature"))?;
    // Time claims are checked below against the caller's clock, as the Python code does.
    let mut validation = Validation::new(Algorithm::RS256);
    validation.validate_exp = false;
    validation.validate_nbf = false;
    validation.validate_aud = false;
    validation.required_spec_claims = HashSet::new();
    let claims = decode::<Value>(token, &key, &validation)
        .map_err(|_| Refusal::Unauthenticated("invalid token signature"))?
        .claims;
    let number = |name: &str| claims.get(name).and_then(Value::as_i64);
    let audience_ok = match claims.get("aud") {
        Some(Value::String(aud)) => aud == audience,
        Some(Value::Array(auds)) => auds.iter().any(|a| a.as_str() == Some(audience)),
        _ => false,
    };
    let times_ok = number("exp").is_some_and(|exp| exp >= now - SKEW)
        && number("nbf").unwrap_or(0) <= now + SKEW
        && number("iat").is_some_and(|iat| now - MAX_AGE <= iat && iat <= now + SKEW);
    if claims.get("iss").and_then(Value::as_str) != Some(ISSUER) || !audience_ok || !times_ok {
        return Err(Refusal::Unauthenticated("token issuer, audience or time claims are invalid"));
    }
    let jti = claims.get("jti").and_then(Value::as_str).filter(|j| !j.is_empty())
        .ok_or(Refusal::Unauthenticated("token has no ID"))?;
    let text = |name: &str| match claims.get(name) {
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
        None => "None".to_owned(),
    };
    let wrong: Vec<&str> = session.expected.iter().filter(|(k, v)| text(k) != **v).map(|(k, _)| k.as_str()).collect();
    if wrong.iter().any(|k| *k == "repository_id" || *k == "repository_owner_id") {
        return Err(Refusal::ForeignRepository);
    }
    if !wrong.is_empty() {
        return Err(Refusal::Recorded(format!("token does not match the session: {}", wrong.join(", "))));
    }
    if text("event_name") != "workflow_dispatch" || text("ref") != session.ref_name {
        return Err(Refusal::Recorded("token does not match the session: event_name, ref".to_owned()));
    }
    if text("runner_environment") != "github-hosted" {
        return Err(Refusal::Recorded("only GitHub-hosted runners are served".to_owned()));
    }
    let run = format!("{}/{}", text("run_id"), text("run_attempt"));
    if text("run_attempt") != "1" || session.run.as_deref().is_some_and(|bound| bound != run) {
        return Err(Refusal::Recorded("session is bound to another run".to_owned()));
    }
    if !seen.insert(jti.to_owned()) {
        return Err(Refusal::Unauthenticated("token already used"));
    }
    Ok((claims, run))
}
