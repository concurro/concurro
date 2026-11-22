//! Edge-facing sign-in, session and logout routes.

use axum::{
    Json, Router,
    extract::{Query, State},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use axum_extra::extract::{
    CookieJar,
    cookie::{Cookie, SameSite},
};
use reqwest::Url;
use serde::Deserialize;
use serde_json::json;
use time::Duration;
use uuid::Uuid;

use crate::{AccountState, github::AccessError, sessions::Sessions};

const LOGIN: &str = "concurro_login";
pub(crate) const SESSION: &str = "concurro_session";
const VIEWER: &str = "concurro_viewer";
const LOGOUT: &str = "concurro_logout";

/// Require the browser's existing custom header on every mutation.
pub(crate) fn csrf(headers: &HeaderMap) -> bool {
    headers
        .get("x-concurro-request")
        .and_then(|value| value.to_str().ok())
        == Some("1")
}

/// Optional destination after successful sign-in.
#[derive(Deserialize)]
struct LoginQuery {
    /// Application-relative destination.
    next: Option<String>,
}

/// GitHub's returned authorization pair.
#[derive(Deserialize)]
struct CallbackQuery {
    /// One-use authorization code.
    code: Option<String>,
    /// Browser-bound state.
    state: Option<String>,
}

/// Browser API error with a stable detail string.
struct ApiError(StatusCode, &'static str);

impl IntoResponse for ApiError {
    /// Encode a browser API error.
    fn into_response(self) -> Response {
        (self.0, Json(json!({"detail":self.1}))).into_response()
    }
}

/// Build only the browser-facing routes.
pub fn router(state: AccountState) -> Router {
    Router::new()
        .route("/auth/login", get(login))
        .route("/auth/callback", get(callback))
        .route("/auth/logout", post(logout))
        .route("/auth/logout-status", get(logout_status))
        .route(
            "/api/repos/{owner}/{repo}/issues/{number}/commands",
            post(crate::commands::command),
        )
        .route(
            "/api/tasks/{owner}/{repo}/{number}/commands",
            post(crate::commands::command),
        )
        .route(
            "/api/health",
            get(|| async { Json(json!({"status":"ok"})) }),
        )
        .route("/api/me", get(me))
        .route(
            "/api/me/credentials",
            axum::routing::any(crate::credentials::credentials),
        )
        .route(
            "/api/me/credentials/{*path}",
            axum::routing::any(crate::credentials::credentials),
        )
        .route("/api/me/relays", get(crate::credentials::relays))
        .with_state(state)
}

/// Issue a browser cookie at the configured public path.
fn cookie(
    name: &'static str,
    value: String,
    state: &AccountState,
    age: Option<i64>,
    lax: bool,
) -> Cookie<'static> {
    let path = if state.base_path.is_empty() {
        "/"
    } else {
        &state.base_path
    };
    let mut builder = Cookie::build((name, value))
        .path(path.to_owned())
        .http_only(true)
        .same_site(if lax { SameSite::Lax } else { SameSite::Strict })
        .secure(state.secure_cookies);
    if let Some(seconds) = age {
        builder = builder.max_age(Duration::seconds(seconds));
    }
    builder.build()
}

/// Remove one browser cookie at the same path where it was issued.
fn clear(name: &'static str, state: &AccountState) -> Cookie<'static> {
    let mut cookie = cookie(name, String::new(), state, Some(0), false);
    cookie.make_removal();
    cookie
}

/// Return a redirect and its cookie changes.
fn redirect(jar: CookieJar, location: String) -> Response {
    (jar, (StatusCode::FOUND, [(header::LOCATION, location)])).into_response()
}

/// Redirect with a URL-encoded sign-in explanation for the frontend's error display.
fn signin_error(jar: CookieJar, state: &AccountState, message: &str) -> Response {
    let url = Url::parse_with_params("https://concurro.invalid/signin", &[("error", message)])
        .expect("fixed sign-in URL");
    redirect(
        jar,
        format!(
            "{}/signin?{}",
            state.base_path,
            url.query().unwrap_or_default()
        ),
    )
}

/// Start a browser-bound state and S256 authorization.
async fn login(
    State(state): State<AccountState>,
    Query(query): Query<LoginQuery>,
    jar: CookieJar,
) -> Response {
    let next = query.next.unwrap_or_else(|| "/".to_owned());
    let next = if next.starts_with('/') && !next.starts_with("//") && !next.contains('\\') {
        next
    } else {
        "/".to_owned()
    };
    let oauth_state = Sessions::random_token();
    let Ok((url, verifier)) = state.oauth.authorize(&oauth_state) else {
        return signin_error(
            jar,
            &state,
            "GitHub sign-in is unavailable; please sign in again",
        );
    };
    let pending = state.sessions.begin(oauth_state, verifier, next);
    redirect(
        jar.add(cookie(LOGIN, pending, &state, Some(600), true)),
        url,
    )
}

/// Consume the pending authorization and create one session.
async fn callback(
    State(state): State<AccountState>,
    Query(query): Query<CallbackQuery>,
    jar: CookieJar,
) -> Response {
    let pending = state
        .sessions
        .consume(jar.get(LOGIN).map(Cookie::value), query.state.as_deref());
    let jar = jar.add(clear(LOGIN, &state));
    let Some(pending) = pending else {
        return signin_error(
            jar,
            &state,
            "sign-in expired or was started in another browser; please sign in again",
        );
    };
    let Some(code) = query.code.as_deref() else {
        return signin_error(jar, &state, "GitHub didn't return a sign-in code");
    };
    let token = match state.oauth.exchange(code, &pending.verifier).await {
        Ok(token) => token,
        Err(message) => return signin_error(jar, &state, message),
    };
    let user = match state.github.user(&token.secret).await {
        Ok(user) => user,
        Err(AccessError::Unavailable) => {
            return signin_error(jar, &state, "GitHub is unavailable; try again");
        }
        Err(_) => return signin_error(jar, &state, "GitHub did not confirm your account"),
    };
    let lifetime = token
        .expires_in
        .unwrap_or(std::time::Duration::from_secs(8 * 3600))
        .as_secs() as i64;
    let session = state.sessions.create(token.secret, user, lifetime);
    let jar = jar
        .add(cookie(SESSION, session.id, &state, Some(lifetime), false))
        .add(cookie(
            VIEWER,
            session.viewer_token,
            &state,
            Some(lifetime),
            false,
        ))
        .add(clear(LOGOUT, &state));
    redirect(jar, format!("{}{}", state.base_path, pending.next))
}

/// Return the current GitHub account identity.
async fn me(
    State(state): State<AccountState>,
    jar: CookieJar,
) -> Result<Json<serde_json::Value>, ApiError> {
    let session = state
        .sessions
        .get(jar.get(SESSION).map(Cookie::value))
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "session_expired"))?;
    let login = &session.user.login;
    let name = session
        .user
        .name
        .as_deref()
        .filter(|name| !name.is_empty())
        .unwrap_or(login);
    let avatar_url = session.user.avatar_url.as_deref().unwrap_or_default();
    Ok(Json(json!({"id":session.user.id,"login":login,
        "name":name,"avatarUrl":avatar_url})))
}

/// End local authority before scheduling remote revocation.
async fn logout(
    State(state): State<AccountState>,
    headers: HeaderMap,
    jar: CookieJar,
) -> Result<Response, ApiError> {
    if !csrf(&headers) {
        return Err(ApiError(StatusCode::FORBIDDEN, "missing request header"));
    }
    let session = state.sessions.end(jar.get(SESSION).map(Cookie::value));
    let old_receipt = jar
        .get(LOGOUT)
        .and_then(|cookie| Uuid::parse_str(cookie.value()).ok());
    let result = if let Some(session) = session {
        let result = state.logout.revoke(&session.token).await;
        (result.receipt, result.remote)
    } else {
        (old_receipt, state.logout.status(old_receipt).await)
    };
    let mut jar = jar.add(clear(SESSION, &state)).add(clear(VIEWER, &state));
    if let Some(receipt) = result.0 {
        jar = jar.add(cookie(LOGOUT, receipt.to_string(), &state, None, false));
    }
    Ok((jar, Json(json!({"signedOut":true,"remote":result.1}))).into_response())
}

/// Read the browser's anonymous revocation receipt.
async fn logout_status(
    State(state): State<AccountState>,
    jar: CookieJar,
) -> Json<serde_json::Value> {
    let receipt = jar
        .get(LOGOUT)
        .and_then(|cookie| Uuid::parse_str(cookie.value()).ok());
    Json(json!({"remote":state.logout.status(receipt).await}))
}
