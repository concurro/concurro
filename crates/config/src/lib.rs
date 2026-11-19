//! Typed TOML settings and file-backed secrets for services.

use std::{env, fs, path::Path};

use anyhow::{Context, Result};
use config::{Config, File, FileFormat};
use serde::de::DeserializeOwned;

/// Service-specific startup checks for parsed settings.
pub trait Validate {
    /// Reject settings that cannot run the service.
    fn validate(&self) -> Result<()>;
}

/// Load and validate a service's TOML file named by an environment variable.
pub fn load<T: DeserializeOwned + Validate>(path_variable: &str) -> Result<T> {
    let path = env::var(path_variable).with_context(|| format!("read {path_variable}"))?;
    load_file(&path)
}

/// Parse one TOML file without coercing its scalar values into field types.
fn load_file<T: DeserializeOwned + Validate>(path: &str) -> Result<T> {
    let value: serde_json::Value = Config::builder()
        .add_source(File::new(path, FileFormat::Toml))
        .build()
        .with_context(|| format!("read {path}"))?
        .try_deserialize()
        .with_context(|| format!("parse {path}"))?;
    let settings: T = serde_json::from_value(value).with_context(|| format!("parse {path}"))?;
    settings.validate()?;
    Ok(settings)
}

/// Read a credential file without logging its content.
pub fn read_secret(path: impl AsRef<Path>) -> Result<String> {
    let path = path.as_ref();
    let value =
        fs::read_to_string(path).with_context(|| format!("read secret at {}", path.display()))?;
    let value = value.trim_end_matches(['\r', '\n']);
    anyhow::ensure!(
        !value.trim().is_empty(),
        "secret at {} is empty",
        path.display()
    );
    Ok(value.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    /// Minimal typed configuration used to detect scalar coercion.
    #[derive(Deserialize)]
    struct TestSettings {
        /// Required integer.
        count: u64,
        /// Required boolean.
        enabled: bool,
        /// Required string.
        url: String,
    }

    impl Validate for TestSettings {
        /// Reject an otherwise valid but unusable configuration.
        fn validate(&self) -> Result<()> {
            anyhow::ensure!(self.count > 0 && self.enabled && !self.url.is_empty());
            Ok(())
        }
    }

    /// Typed loading keeps TOML scalar types and runs service validation.
    #[test]
    fn config_file_rejects_scalar_coercion() {
        let path = env::temp_dir().join(format!("concurro-config-test-{}", std::process::id()));
        let path_name = path.to_str().expect("UTF-8 temporary path");
        fs::write(&path, "count = 2\nenabled = true\nurl = 'nats://server'\n")
            .expect("valid config file");
        let valid: TestSettings = load_file(path_name).expect("valid config");
        assert_eq!(valid.count, 2);
        for invalid in [
            "count = '2'\nenabled = true\nurl = 'nats://server'\n",
            "count = 2\nenabled = 'true'\nurl = 'nats://server'\n",
            "count = 2\nenabled = true\nurl = 4222\n",
            "count = 0\nenabled = true\nurl = 'nats://server'\n",
        ] {
            fs::write(&path, invalid).expect("invalid config file");
            assert!(load_file::<TestSettings>(path_name).is_err());
        }
        fs::remove_file(path).expect("remove test file");
    }

    /// Empty files cannot become usable webhook or bearer credentials.
    #[test]
    fn secret_requires_nonempty_content() {
        let path = env::temp_dir().join(format!("concurro-secret-test-{}", std::process::id()));
        fs::write(&path, " \n").expect("empty secret file");
        assert!(read_secret(&path).is_err());
        fs::write(&path, "credential\n").expect("populated secret file");
        assert_eq!(read_secret(&path).expect("secret"), "credential");
        fs::remove_file(path).expect("remove test file");
    }
}
