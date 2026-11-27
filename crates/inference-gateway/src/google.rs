//! Gemini request paths and subscription envelopes.

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

/// Canonical model resource and operation captured before admission.
pub struct Request {
    /// Model identity shared by grants and reviewer identity.
    pub model: String,
    /// Gemini API path, including its explicit streaming query.
    pub path: String,
    /// Whether the caller requested SSE.
    pub streaming: bool,
}

impl Request {
    /// Parse one supported Gemini operation without accepting query credentials.
    pub fn parse(path: &str, query: Option<&str>) -> Result<Self> {
        let (model, operation) = path
            .strip_prefix("models/")
            .context("Gemini model resource missing")?
            .split_once(':')
            .context("Gemini operation missing")?;
        ensure!(
            !model.is_empty()
                && model
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"-_.".contains(&byte)),
            "invalid Gemini model resource"
        );
        let streaming = match operation {
            "streamGenerateContent" => true,
            "generateContent" => false,
            _ => anyhow::bail!("unsupported Gemini operation"),
        };
        ensure!(
            if streaming {
                query == Some("alt=sse")
            } else {
                query.is_none()
            },
            "unsupported Gemini query"
        );
        Ok(Self {
            model: model.to_owned(),
            path: format!("v1beta/{path}{}", if streaming { "?alt=sse" } else { "" }),
            streaming,
        })
    }

    /// Wrap API JSON while leaving conversation content and signatures unchanged.
    pub fn subscription(&self, body: &[u8], request_id: &str) -> Result<Vec<u8>> {
        ensure!(
            self.streaming,
            "Antigravity subscription requires streaming GenerateContent"
        );
        let request: Value = serde_json::from_slice(body).context("Gemini request is not JSON")?;
        ensure!(request.is_object(), "Gemini request must be an object");
        Ok(serde_json::to_vec(
            &json!({"model":self.model,"requestId":request_id,"request":request,"userAgent":"antigravity","requestType":"agent"}),
        )?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A model URI cannot carry a second authority, query key or arbitrary method.
    #[test]
    fn model_operation_and_query_are_explicit() {
        let request = Request::parse(
            "models/gemini-4-neon:streamGenerateContent",
            Some("alt=sse"),
        )
        .unwrap();
        assert_eq!(request.model, "gemini-4-neon");
        assert_eq!(
            request.path,
            "v1beta/models/gemini-4-neon:streamGenerateContent?alt=sse"
        );
        for query in [None, Some("alt=sse&key=caller"), Some("key=caller")] {
            assert!(Request::parse("models/gemini-4-neon:streamGenerateContent", query).is_err());
        }
        assert!(Request::parse("models/a/b:streamGenerateContent", Some("alt=sse")).is_err());
        let original = json!({"contents":[{"role":"model","parts":[{"thoughtSignature":"signed","functionCall":{"name":"read_file","args":{}}}]}],"generationConfig":{"thinkingConfig":{"thinkingLevel":"LOW"}}});
        let wrapped: Value = serde_json::from_slice(
            &request
                .subscription(&serde_json::to_vec(&original).unwrap(), "request-1")
                .unwrap(),
        )
        .unwrap();
        assert_eq!(wrapped["request"], original);
        assert!(wrapped.get("project").is_none());
    }
}
