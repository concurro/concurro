use std::collections::{BTreeMap, HashSet};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use jsonwebtoken::{EncodingKey, Header, jwk::JwkSet};
use rsa::{RsaPrivateKey, pkcs1::EncodeRsaPrivateKey, traits::PublicKeyParts};
use s0_oidc::{ISSUER, Refusal, Session, verify};
use serde_json::{Value, json};

const NOW: i64 = 1_800_000_000;

struct Issuer {
    key: EncodingKey,
    jwks: JwkSet,
}

fn issuer() -> Issuer {
    let private = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).unwrap();
    let public = private.to_public_key();
    let jwks: JwkSet = serde_json::from_value(json!({"keys": [{
        "kty": "RSA", "alg": "RS256", "use": "sig", "kid": "gh-1",
        "n": URL_SAFE_NO_PAD.encode(public.n().to_bytes_be()),
        "e": URL_SAFE_NO_PAD.encode(public.e().to_bytes_be()),
    }]}))
    .unwrap();
    let pem = private.to_pkcs1_pem(Default::default()).unwrap();
    Issuer { key: EncodingKey::from_rsa_pem(pem.as_bytes()).unwrap(), jwks }
}

fn claims() -> Value {
    json!({
        "iss": ISSUER, "aud": "concurro-gateway", "iat": NOW - 5, "nbf": NOW - 5, "exp": NOW + 300,
        "jti": "jti-1", "repository_id": "11", "repository_owner_id": "22",
        "job_workflow_ref": "concurro/concurro/.github/workflows/agent.yml@refs/tags/v2",
        "actor_id": "33", "event_name": "workflow_dispatch",
        "ref": "refs/heads/concurro/runs/s1", "runner_environment": "github-hosted",
        "run_id": "900", "run_attempt": "1",
    })
}

fn session() -> Session {
    let expected = BTreeMap::from([
        ("repository_id".to_owned(), "11".to_owned()),
        ("repository_owner_id".to_owned(), "22".to_owned()),
        ("job_workflow_ref".to_owned(), "concurro/concurro/.github/workflows/agent.yml@refs/tags/v2".to_owned()),
        ("actor_id".to_owned(), "33".to_owned()),
    ]);
    Session { expected, ref_name: "refs/heads/concurro/runs/s1".to_owned(), run: None }
}

fn token(issuer: &Issuer, claims: &Value, kid: &str) -> String {
    let mut header = Header::new(jsonwebtoken::Algorithm::RS256);
    header.kid = Some(kid.to_owned());
    jsonwebtoken::encode(&header, claims, &issuer.key).unwrap()
}

fn check(issuer: &Issuer, claims: Value, session: &Session) -> Result<String, Refusal> {
    verify(&token(issuer, &claims, "gh-1"), &issuer.jwks, "concurro-gateway", session, &mut HashSet::new(), NOW)
        .map(|(_, run)| run)
}

fn with(field: &str, value: Value) -> Value {
    let mut c = claims();
    c[field] = value;
    c
}

#[test]
fn a_valid_token_binds_its_run() {
    let issuer = issuer();
    assert_eq!(check(&issuer, claims(), &session()), Ok("900/1".to_owned()));
}

#[test]
fn signature_issuer_audience_and_time_rules() {
    let issuer = issuer();
    let other = self::issuer();
    let forged = token(&other, &claims(), "gh-1");
    assert!(matches!(verify(&forged, &issuer.jwks, "concurro-gateway", &session(), &mut HashSet::new(), NOW), Err(Refusal::Unauthenticated(_))));
    let unknown_kid = token(&issuer, &claims(), "gh-2");
    assert!(matches!(verify(&unknown_kid, &issuer.jwks, "concurro-gateway", &session(), &mut HashSet::new(), NOW), Err(Refusal::Unauthenticated(_))));
    for bad in [
        with("iss", json!("https://example.com")),
        with("aud", json!("someone-else")),
        with("exp", json!(NOW - 61)),
        with("nbf", json!(NOW + 61)),
        with("iat", json!(NOW - 601)),
        with("iat", json!(NOW + 61)),
        with("jti", json!("")),
    ] {
        assert!(matches!(check(&issuer, bad, &session()), Err(Refusal::Unauthenticated(_))));
    }
    // Inside the skew still passes.
    assert!(check(&issuer, with("exp", json!(NOW - 59)), &session()).is_ok());
    assert!(check(&issuer, with("aud", json!(["x", "concurro-gateway"])), &session()).is_ok());
}

#[test]
fn another_repository_is_refused_without_recording() {
    let issuer = issuer();
    assert_eq!(check(&issuer, with("repository_id", json!("99")), &session()), Err(Refusal::ForeignRepository));
    assert_eq!(check(&issuer, with("repository_owner_id", json!("99")), &session()), Err(Refusal::ForeignRepository));
}

#[test]
fn other_mismatches_are_recorded() {
    let issuer = issuer();
    for (field, value) in [
        ("job_workflow_ref", json!("concurro/concurro/.github/workflows/agent.yml@refs/heads/main")),
        ("actor_id", json!("44")),
        ("event_name", json!("push")),
        ("ref", json!("refs/heads/main")),
        ("runner_environment", json!("self-hosted")),
        ("run_attempt", json!("2")),
    ] {
        assert!(matches!(check(&issuer, with(field, value), &session()), Err(Refusal::Recorded(_))), "{field}");
    }
}

#[test]
fn a_bound_session_refuses_another_run_and_a_reused_jti() {
    let issuer = issuer();
    let mut bound = session();
    bound.run = Some("900/1".to_owned());
    assert!(check(&issuer, claims(), &bound).is_ok());
    assert!(matches!(check(&issuer, with("run_id", json!("901")), &bound), Err(Refusal::Recorded(_))));
    let mut seen = HashSet::new();
    let once = token(&issuer, &claims(), "gh-1");
    assert!(verify(&once, &issuer.jwks, "concurro-gateway", &bound, &mut seen, NOW).is_ok());
    assert_eq!(verify(&once, &issuer.jwks, "concurro-gateway", &bound, &mut seen, NOW).map(|_| ()), Err(Refusal::Unauthenticated("token already used")));
    // A fresh token from the same run (new jti) is accepted: the v2 repeat claim.
    let again = token(&issuer, &with("jti", json!("jti-2")), "gh-1");
    assert!(verify(&again, &issuer.jwks, "concurro-gateway", &bound, &mut seen, NOW).is_ok());
}
