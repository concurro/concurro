//! Browser-bound pending logins and short-lived, memory-only user sessions.

use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};

use crate::github::User;

const LOGIN_SECONDS: i64 = 600;

/// One unconsumed authorization bound to the starting browser.
pub struct PendingLogin {
    /// One-use OAuth state.
    pub state: String,
    /// Private S256 verifier.
    pub verifier: String,
    /// Validated application-relative destination.
    pub next: String,
    expires_at: i64,
}

/// One signed-in user and server-only GitHub access token.
#[derive(Clone)]
pub struct Session {
    /// Browser session identity.
    pub id: String,
    /// Read-only viewer identity for the separate read API.
    pub viewer_token: String,
    /// Confirmed GitHub user.
    pub user: User,
    /// Token kept only in process memory.
    pub token: String,
    /// Expiration epoch second.
    pub expires_at: i64,
}

/// Local indexes for pending sign-ins and active sessions.
#[derive(Default)]
struct Inner {
    /// One-use browser login cookies.
    pending: HashMap<String, PendingLogin>,
    /// Browser session ID to authority.
    sessions: HashMap<String, Session>,
    /// Viewer token to browser session ID.
    viewers: HashMap<String, String>,
}

/// Shared in-memory authority with an injectable clock for expiry tests.
#[derive(Clone)]
pub struct Sessions {
    inner: Arc<Mutex<Inner>>,
    clock: Arc<dyn Fn() -> i64 + Send + Sync>,
}

impl Default for Sessions {
    /// Use the system clock for ordinary browser sessions.
    fn default() -> Self {
        Self::new()
    }
}

impl Sessions {
    /// Use the system UTC clock in production.
    pub fn new() -> Self {
        Self::with_clock(Arc::new(|| chrono::Utc::now().timestamp()))
    }

    /// Supply a clock for deterministic login and token expiry.
    pub fn with_clock(clock: Arc<dyn Fn() -> i64 + Send + Sync>) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner::default())),
            clock,
        }
    }

    /// Current epoch second used by both pending logins and sessions.
    pub fn now(&self) -> i64 {
        (self.clock)()
    }

    /// Mint an opaque browser credential.
    pub fn random_token() -> String {
        URL_SAFE_NO_PAD.encode(rand::random::<[u8; 32]>())
    }

    /// Retain one browser-bound PKCE exchange for ten minutes.
    pub fn begin(&self, state: String, verifier: String, next: String) -> String {
        let cookie = Self::random_token();
        let now = self.now();
        let mut inner = self.inner.lock().expect("session lock");
        inner.pending.retain(|_, pending| pending.expires_at > now);
        inner.pending.insert(
            cookie.clone(),
            PendingLogin {
                state,
                verifier,
                next,
                expires_at: now + LOGIN_SECONDS,
            },
        );
        cookie
    }

    /// Consume a pending exchange once, even when state or time is invalid.
    pub fn consume(&self, cookie: Option<&str>, state: Option<&str>) -> Option<PendingLogin> {
        let pending = self
            .inner
            .lock()
            .expect("session lock")
            .pending
            .remove(cookie?)?;
        (pending.expires_at > self.now() && Some(pending.state.as_str()) == state)
            .then_some(pending)
    }

    /// Bind a confirmed token to independent browser and viewer credentials.
    pub fn create(&self, token: String, user: User, expires_in: i64) -> Session {
        let now = self.now();
        let id = Self::random_token();
        let viewer_token = Self::random_token();
        let session = Session {
            id: id.clone(),
            viewer_token: viewer_token.clone(),
            user,
            token,
            expires_at: now + expires_in,
        };
        let mut inner = self.inner.lock().expect("session lock");
        let Inner {
            sessions, viewers, ..
        } = &mut *inner;
        sessions.retain(|_, active| active.expires_at > now);
        viewers.retain(|viewer, session| {
            sessions
                .get(session)
                .is_some_and(|active| active.viewer_token == *viewer)
        });
        inner.viewers.insert(viewer_token, id.clone());
        inner.sessions.insert(id, session.clone());
        session
    }

    /// Resolve a current browser session, removing expired authority.
    pub fn get(&self, id: Option<&str>) -> Option<Session> {
        let id = id?;
        let mut inner = self.inner.lock().expect("session lock");
        let session = inner.sessions.get(id)?.clone();
        if session.expires_at <= self.now() {
            inner.sessions.remove(id);
            inner.viewers.remove(&session.viewer_token);
            None
        } else {
            Some(session)
        }
    }

    /// Resolve a current viewer token through its distinct index.
    pub fn viewer(&self, token: Option<&str>) -> Option<Session> {
        let id = self
            .inner
            .lock()
            .expect("session lock")
            .viewers
            .get(token?)
            .cloned()?;
        self.get(Some(&id))
    }

    /// End local authority before any remote revocation work.
    pub fn end(&self, id: Option<&str>) -> Option<Session> {
        let mut inner = self.inner.lock().expect("session lock");
        let session = inner.sessions.remove(id?)?;
        inner.viewers.remove(&session.viewer_token);
        Some(session)
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{
        Arc,
        atomic::{AtomicI64, Ordering},
    };

    use super::{Sessions, User};

    /// New logins and sessions discard expired entries and their viewer indexes.
    #[test]
    fn new_authority_prunes_expired_entries() {
        let now = Arc::new(AtomicI64::new(1000));
        let clock = now.clone();
        let sessions = Sessions::with_clock(Arc::new(move || clock.load(Ordering::SeqCst)));
        let old_login = sessions.begin("old".into(), "verifier".into(), "/".into());
        let user = User {
            id: 1,
            login: "reader".into(),
            name: None,
            avatar_url: None,
        };
        let old_session = sessions.create("old-token".into(), user.clone(), 10);
        now.store(1601, Ordering::SeqCst);
        sessions.begin("new".into(), "verifier".into(), "/".into());
        let current = sessions.create("new-token".into(), user, 10);
        let inner = sessions.inner.lock().unwrap();
        assert_eq!(inner.pending.len(), 1);
        assert!(!inner.pending.contains_key(&old_login));
        assert_eq!(inner.sessions.len(), 1);
        assert!(!inner.sessions.contains_key(&old_session.id));
        assert_eq!(inner.viewers.len(), 1);
        assert!(!inner.viewers.contains_key(&old_session.viewer_token));
        assert_eq!(inner.viewers.get(&current.viewer_token), Some(&current.id));
    }
}
