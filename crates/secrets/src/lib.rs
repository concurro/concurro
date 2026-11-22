//! OpenBao and file-backed secrets for Concurro services.
//!
//! [`OpenBao`] signs in with an AppRole, keeps its token renewed, and reads, creates and
//! destroys KV v2 secrets. [`Secrets`] resolves a [`SecretRef`] setting from OpenBao or a
//! file, so a setting can move between the two without code changes. Secret values are
//! held in [`SecretString`] and never appear in errors or logs.

mod client;
mod error;
mod reference;

pub use client::{Metadata, OpenBao, OpenBaoSettings, Secret};
pub use error::Error;
pub use reference::{SecretRef, Secrets};
pub use secrecy::{ExposeSecret, SecretString};
