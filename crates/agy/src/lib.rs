//! Antigravity CLI model selection and runner adapter.

#[cfg(feature = "runner")]
pub mod runner;

use anyhow::{Result, bail, ensure};

/// Validate a canonical Gemini model and explicit effort without selecting a replacement.
pub fn validate_model(model: &str, effort: &str) -> Result<()> {
    let efforts: &[&str] = match model {
        "gemini-4-neon" | "gemini-4-hydrogen" => &["low", "medium", "high", "xhigh"],
        "gemini-4-carbon" | "gemini-4-argon" | "gemini-3.8-flash" => &["low", "medium", "high"],
        _ => bail!("unsupported Antigravity canonical model {model}"),
    };
    ensure!(
        efforts.contains(&effort),
        "Antigravity model {model} does not support effort {effort}"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::validate_model;

    /// Effort changes selection without changing the canonical model identity.
    #[test]
    fn canonical_model_selection() {
        for effort in ["low", "medium", "high", "xhigh"] {
            validate_model("gemini-4-neon", effort).unwrap();
        }
        for (model, effort) in [
            ("gemini-4-neon", "max"),
            ("gemini-4-carbon", "xhigh"),
            ("gemini-4-neon-low", "low"),
            ("unknown", "low"),
        ] {
            assert!(validate_model(model, effort).is_err());
        }
    }
}
