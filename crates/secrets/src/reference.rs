//! A secret setting that names either an OpenBao field or a file.

use std::{
    collections::HashMap,
    fmt,
    path::PathBuf,
    str::FromStr,
    sync::Mutex,
    time::{Duration, Instant},
};

use secrecy::SecretString;
use serde::{Deserialize, Deserializer};

use crate::{Error, OpenBao};

/// Where a secret setting's value lives.
///
/// Written as `openbao:<path>#<field>`, such as
/// `openbao:ops/gateway/providers#chatgpt_access_token`, or as a file path.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum SecretRef {
    /// A file holding the value, read as [`concurro_config::read_secret`] does.
    File(PathBuf),
    /// One field of a KV v2 secret in OpenBao.
    OpenBao {
        /// The secret's path under the mount.
        path: String,
        /// The field holding the value.
        field: String,
    },
}

impl FromStr for SecretRef {
    type Err = Error;

    fn from_str(value: &str) -> Result<Self, Error> {
        let Some(reference) = value.strip_prefix("openbao:") else {
            if value.trim().is_empty() {
                return Err(Error::Settings("empty secret file path".to_owned()));
            }
            return Ok(Self::File(PathBuf::from(value)));
        };
        match reference.split_once('#') {
            Some((path, field))
                if !path.is_empty()
                    && !field.is_empty()
                    && !path.starts_with('/')
                    && !field.contains('#') =>
            {
                Ok(Self::OpenBao {
                    path: path.to_owned(),
                    field: field.to_owned(),
                })
            }
            _ => Err(Error::Settings(format!(
                "{value:?} is not openbao:<path>#<field>"
            ))),
        }
    }
}

impl<'de> Deserialize<'de> for SecretRef {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer)?
            .parse()
            .map_err(serde::de::Error::custom)
    }
}

impl fmt::Display for SecretRef {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::File(path) => write!(formatter, "{}", path.display()),
            Self::OpenBao { path, field } => write!(formatter, "openbao:{path}#{field}"),
        }
    }
}

/// Resolves [`SecretRef`] settings, keeping each value in memory for the refresh
/// interval.
pub struct Secrets {
    openbao: Option<OpenBao>,
    refresh: Duration,
    files: Mutex<HashMap<PathBuf, (Instant, SecretString)>>,
}

impl fmt::Debug for Secrets {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Secrets")
            .field("openbao", &self.openbao)
            .field("refresh", &self.refresh)
            .finish_non_exhaustive()
    }
}

impl Secrets {
    /// Resolve file references, and OpenBao ones when a client is given. File values are
    /// read again after `refresh`, as OpenBao values are after the client's interval.
    pub fn new(openbao: Option<OpenBao>, refresh: Duration) -> Self {
        Self {
            openbao,
            refresh,
            files: Mutex::new(HashMap::new()),
        }
    }

    /// The setting's current value.
    pub async fn get(&self, reference: &SecretRef) -> Result<SecretString, Error> {
        self.resolve(reference, false).await
    }

    /// The setting's value read from its source now, for a caller that has just had it
    /// refused.
    pub async fn refresh(&self, reference: &SecretRef) -> Result<SecretString, Error> {
        self.resolve(reference, true).await
    }

    async fn resolve(&self, reference: &SecretRef, fresh: bool) -> Result<SecretString, Error> {
        match reference {
            SecretRef::File(path) => {
                if !fresh
                    && let Some((read, value)) = self.files.lock().expect("file cache").get(path)
                    && read.elapsed() < self.refresh
                {
                    return Ok(value.clone());
                }
                let value = SecretString::from(
                    concurro_config::read_secret(path)
                        .map_err(|_| Error::File(path.display().to_string()))?,
                );
                self.files
                    .lock()
                    .expect("file cache")
                    .insert(path.clone(), (Instant::now(), value.clone()));
                Ok(value)
            }
            SecretRef::OpenBao { path, field } => {
                let openbao = self.openbao.as_ref().ok_or_else(|| {
                    Error::Settings(format!("{reference} needs OpenBao settings"))
                })?;
                let secret = if fresh {
                    openbao.refresh(path).await?
                } else {
                    openbao.read(path).await?
                };
                Ok(secret.field(field)?.clone())
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::{assert_matches, env, fs};

    use secrecy::ExposeSecret;

    use super::*;

    #[test]
    fn references_parse_from_settings_strings() {
        assert_eq!(
            "openbao:ops/gateway/providers#chatgpt_access_token"
                .parse::<SecretRef>()
                .unwrap(),
            SecretRef::OpenBao {
                path: "ops/gateway/providers".to_owned(),
                field: "chatgpt_access_token".to_owned(),
            }
        );
        assert_eq!(
            "/etc/concurro/tasks/database-url"
                .parse::<SecretRef>()
                .unwrap(),
            SecretRef::File(PathBuf::from("/etc/concurro/tasks/database-url"))
        );
        for invalid in [
            "openbao:",
            "openbao:ops/x",
            "openbao:#field",
            "openbao:ops/x#",
            "openbao:/ops/x#field",
            "openbao:ops/x#a#b",
            " ",
        ] {
            assert_matches!(
                invalid.parse::<SecretRef>(),
                Err(Error::Settings(_)),
                "{invalid}"
            );
        }
        let reference = SecretRef::OpenBao {
            path: "ops/a".to_owned(),
            field: "b".to_owned(),
        };
        assert_eq!(
            reference.to_string().parse::<SecretRef>().unwrap(),
            reference
        );
    }

    #[tokio::test]
    async fn file_values_refresh_after_the_interval_or_on_request() {
        let path = env::temp_dir().join(format!("concurro-secrets-{}", std::process::id()));
        fs::write(&path, "first\n").unwrap();
        let reference = SecretRef::File(path.clone());
        let secrets = Secrets::new(None, Duration::from_secs(3600));
        assert_eq!(
            secrets.get(&reference).await.unwrap().expose_secret(),
            "first"
        );
        fs::write(&path, "second\n").unwrap();
        assert_eq!(
            secrets.get(&reference).await.unwrap().expose_secret(),
            "first"
        );
        assert_eq!(
            secrets.refresh(&reference).await.unwrap().expose_secret(),
            "second"
        );
        let short = Secrets::new(None, Duration::ZERO);
        fs::write(&path, "third\n").unwrap();
        assert_eq!(
            short.get(&reference).await.unwrap().expose_secret(),
            "third"
        );
        fs::remove_file(path).unwrap();
    }

    #[tokio::test]
    async fn openbao_references_need_a_client() {
        let secrets = Secrets::new(None, Duration::from_secs(60));
        let reference: SecretRef = "openbao:ops/a#b".parse().unwrap();
        assert_matches!(secrets.get(&reference).await, Err(Error::Settings(_)));
    }

    #[test]
    fn errors_and_debug_output_carry_no_values() {
        let value = SecretString::from("sk-known-secret-value");
        assert!(!format!("{value:?}").contains("sk-known-secret-value"));
        let missing = Error::File("/etc/x".to_owned());
        assert!(!missing.to_string().contains("sk-known"));
    }
}
