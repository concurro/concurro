//! Errors that name the path involved but never a secret value or a response body.

/// A secrets operation failed.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    /// OpenBao couldn't be reached, or answered with an unexpected status.
    #[error("OpenBao unavailable for {path}{}", status.map(|s| format!(": HTTP {s}")).unwrap_or_default())]
    Unavailable {
        /// The secret path, or `auth` for sign-in.
        path: String,
        /// The HTTP status OpenBao answered with, when it answered.
        status: Option<u16>,
    },
    /// OpenBao refused the operation: the policy doesn't allow it, or a create found the
    /// path already in use.
    #[error("OpenBao refused the operation on {path}")]
    Refused {
        /// The secret path.
        path: String,
    },
    /// The path holds no readable version.
    #[error("no secret at {path}")]
    NotFound {
        /// The secret path.
        path: String,
    },
    /// The secret exists but lacks the requested field.
    #[error("secret at {path} has no field {field}")]
    MissingField {
        /// The secret path.
        path: String,
        /// The missing field.
        field: String,
    },
    /// A file-backed secret couldn't be read.
    #[error("secret file unreadable: {0}")]
    File(String),
    /// A setting is malformed.
    #[error("invalid secret setting: {0}")]
    Settings(String),
}
