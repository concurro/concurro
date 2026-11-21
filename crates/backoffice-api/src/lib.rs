//! Read-only backoffice router reusable by local authority integration tests.

pub mod api;
pub mod auth;
pub mod model;

pub use concurro_backoffice_postgres::Target;
