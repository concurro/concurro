//! GitHub OAuth authorization and token exchange over the shared HTTP client.

use std::{io, time::Duration};

use anyhow::Result;
use oauth2::{
    AuthType, AuthUrl, AuthorizationCode, ClientId, ClientSecret, CsrfToken, HttpRequest,
    PkceCodeChallenge, PkceCodeVerifier, RedirectUrl, RequestTokenError, TokenResponse, TokenUrl,
    basic::BasicClient,
};
use reqwest::Client;

/// OAuth endpoints and credentials; the token remains server-side.
#[derive(Clone)]
pub struct OAuth {
    client: Client,
    client_id: String,
    client_secret: String,
    redirect: String,
    authorize_url: String,
    token_url: String,
    api_base: String,
}

/// One GitHub access token and its supplied lifetime.
pub struct AccessToken {
    /// Secret access token; never serialize or log it.
    pub secret: String,
    /// GitHub-supplied lifetime, if present.
    pub expires_in: Option<Duration>,
}

impl OAuth {
    /// Bind OAuth credentials to explicit endpoints, which local tests can fake.
    pub fn new(
        client: Client,
        client_id: String,
        client_secret: String,
        redirect: String,
        authorize_url: String,
        token_url: String,
        api_base: String,
    ) -> Self {
        Self {
            client,
            client_id,
            client_secret,
            redirect,
            authorize_url,
            token_url,
            api_base,
        }
    }

    /// Build a no-scope GitHub authorization URL and its private PKCE verifier.
    pub fn authorize(&self, state: &str) -> Result<(String, String)> {
        let oauth = BasicClient::new(ClientId::new(self.client_id.clone()))
            .set_client_secret(ClientSecret::new(self.client_secret.clone()))
            .set_auth_type(AuthType::RequestBody)
            .set_auth_uri(AuthUrl::new(self.authorize_url.clone())?)
            .set_token_uri(TokenUrl::new(self.token_url.clone())?)
            .set_redirect_uri(RedirectUrl::new(self.redirect.clone())?);
        let (challenge, verifier) = PkceCodeChallenge::new_random_sha256();
        let (url, _) = oauth
            .authorize_url(|| CsrfToken::new(state.to_owned()))
            .set_pkce_challenge(challenge)
            .url();
        Ok((url.to_string(), verifier.secret().to_owned()))
    }

    /// Exchange a single-use code with the verifier from its pending login.
    pub async fn exchange(
        &self,
        code: &str,
        verifier: &str,
    ) -> std::result::Result<AccessToken, &'static str> {
        let oauth = BasicClient::new(ClientId::new(self.client_id.clone()))
            .set_client_secret(ClientSecret::new(self.client_secret.clone()))
            .set_auth_type(AuthType::RequestBody)
            .set_auth_uri(
                AuthUrl::new(self.authorize_url.clone())
                    .map_err(|_| "GitHub sign-in is unavailable; please sign in again")?,
            )
            .set_token_uri(
                TokenUrl::new(self.token_url.clone())
                    .map_err(|_| "GitHub sign-in is unavailable; please sign in again")?,
            )
            .set_redirect_uri(
                RedirectUrl::new(self.redirect.clone())
                    .map_err(|_| "GitHub sign-in is unavailable; please sign in again")?,
            );
        let client = self.client.clone();
        let adapter = move |request: HttpRequest| {
            let client = client.clone();
            async move {
                let response = client
                    .request(request.method().clone(), request.uri().to_string())
                    .headers(request.headers().clone())
                    .body(request.body().clone())
                    .send()
                    .await
                    .map_err(io::Error::other)?;
                let mut builder = oauth2::http::Response::builder().status(response.status());
                for (name, value) in response.headers() {
                    builder = builder.header(name, value);
                }
                builder
                    .body(response.bytes().await.map_err(io::Error::other)?.to_vec())
                    .map_err(io::Error::other)
            }
        };
        let token = oauth
            .exchange_code(AuthorizationCode::new(code.to_owned()))
            .set_pkce_verifier(PkceCodeVerifier::new(verifier.to_owned()))
            .request_async(&adapter)
            .await
            .map_err(|error| match error {
                RequestTokenError::ServerResponse(_) => {
                    "GitHub refused the sign-in; please sign in again"
                }
                RequestTokenError::Request(_) => {
                    "GitHub sign-in is unavailable; please sign in again"
                }
                RequestTokenError::Parse(_, body) => {
                    match serde_json::from_slice::<serde_json::Value>(&body) {
                        Ok(value)
                            if value
                                .get("error")
                                .and_then(serde_json::Value::as_str)
                                .is_some() =>
                        {
                            "GitHub refused the sign-in; please sign in again"
                        }
                        Ok(_) => "GitHub returned an incomplete sign-in; please sign in again",
                        Err(_) => "GitHub sign-in is unavailable; please sign in again",
                    }
                }
                RequestTokenError::Other(_) => {
                    "GitHub sign-in is unavailable; please sign in again"
                }
            })?;
        Ok(AccessToken {
            secret: token.access_token().secret().to_owned(),
            expires_in: token.expires_in(),
        })
    }

    /// Revoke exactly one OAuth token, confirming GitHub's ambiguous 404/422 responses.
    pub async fn revoke(&self, token: &str) -> bool {
        let url = format!(
            "{}/applications/{}/token",
            self.api_base.trim_end_matches('/'),
            self.client_id
        );
        let request = |method| {
            self.client
                .request(method, &url)
                .basic_auth(&self.client_id, Some(&self.client_secret))
                .header("Accept", "application/vnd.github+json")
                .json(&serde_json::json!({"access_token":token}))
        };
        let Ok(response) = request(reqwest::Method::DELETE).send().await else {
            return false;
        };
        if response.status() == reqwest::StatusCode::NO_CONTENT {
            return true;
        }
        if !matches!(
            response.status(),
            reqwest::StatusCode::NOT_FOUND | reqwest::StatusCode::UNPROCESSABLE_ENTITY
        ) {
            return false;
        }
        request(reqwest::Method::POST)
            .send()
            .await
            .is_ok_and(|response| response.status() == reqwest::StatusCode::NOT_FOUND)
    }
}
