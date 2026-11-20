//! Pinned target configuration shared by task initialization and later decisions.

use std::{collections::BTreeSet, fmt};

use anyhow::{Result, bail, ensure};
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::Value;
use yaml_rust2::scanner::{Scanner, TokenType};

/// M1 settings extracted from the target's exact configuration file.
#[derive(Clone, Debug)]
pub struct TargetConfig {
    /// Entire decoded configuration retained for later stages and payload construction.
    pub raw: Value,
    /// Fork repository named by the target.
    pub repository: String,
    /// Upstream branch selected for source and policy reads.
    pub base: String,
    /// App bot account expected by the target.
    pub bot_login: String,
    /// Maximum charged model calls.
    pub max_model_calls: u64,
    /// Maximum implementation fix rounds.
    pub max_fix_rounds: u64,
    /// Maximum plan revisions.
    pub max_plan_revisions: u64,
    /// Policy file paths read from the pinned upstream base.
    pub policy_documents: Vec<String>,
}

/// Validate duplicate and non-string keys without converting scalar values.
struct StrictValue;

impl<'de> Deserialize<'de> for StrictValue {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct StrictVisitor;

        impl<'de> Visitor<'de> for StrictVisitor {
            type Value = StrictValue;

            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("a JSON-compatible scalar, sequence, or mapping")
            }

            fn visit_bool<E: de::Error>(self, value: bool) -> Result<Self::Value, E> {
                let _ = value;
                Ok(StrictValue)
            }

            fn visit_i64<E: de::Error>(self, value: i64) -> Result<Self::Value, E> {
                let _ = value;
                Ok(StrictValue)
            }

            fn visit_u64<E: de::Error>(self, value: u64) -> Result<Self::Value, E> {
                let _ = value;
                Ok(StrictValue)
            }

            fn visit_f64<E: de::Error>(self, value: f64) -> Result<Self::Value, E> {
                if value.is_finite() {
                    Ok(StrictValue)
                } else {
                    Err(E::custom("non-finite configuration number"))
                }
            }

            fn visit_str<E: de::Error>(self, value: &str) -> Result<Self::Value, E> {
                let _ = value;
                Ok(StrictValue)
            }

            fn visit_string<E: de::Error>(self, value: String) -> Result<Self::Value, E> {
                let _ = value;
                Ok(StrictValue)
            }

            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(StrictValue)
            }

            fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
                self.visit_unit()
            }

            fn visit_some<D: Deserializer<'de>>(
                self,
                deserializer: D,
            ) -> Result<Self::Value, D::Error> {
                StrictValue::deserialize(deserializer)
            }

            fn visit_seq<A: SeqAccess<'de>>(
                self,
                mut sequence: A,
            ) -> Result<Self::Value, A::Error> {
                while sequence.next_element::<StrictValue>()?.is_some() {}
                Ok(StrictValue)
            }

            fn visit_map<A: MapAccess<'de>>(self, mut mapping: A) -> Result<Self::Value, A::Error> {
                let mut keys = BTreeSet::new();
                while let Some((key, StrictValue)) = mapping.next_entry::<String, StrictValue>()? {
                    if !keys.insert(key) {
                        return Err(de::Error::custom("duplicate configuration key"));
                    }
                }
                Ok(StrictValue)
            }
        }

        deserializer.deserialize_any(StrictVisitor)
    }
}

/// Decode the single pinned JSON or YAML configuration and its M1 fields.
pub fn decode_target_config(
    json: Option<&str>,
    yaml: Option<&str>,
    fork: &str,
    bot_login: &str,
    engine_sha: &str,
) -> Result<TargetConfig> {
    let raw = match (json, yaml) {
        (Some(text), None) => {
            serde_json::from_str::<StrictValue>(text).map_err(|_| {
                anyhow::anyhow!("invalid target configuration: .concurro/config.json")
            })?;
            serde_json::from_str::<Value>(text).map_err(|_| {
                anyhow::anyhow!("invalid target configuration: .concurro/config.json")
            })?
        }
        (None, Some(text)) => {
            let mut scanner = Scanner::new(text.chars());
            while let Some(token) = scanner.next_token().map_err(|_| {
                anyhow::anyhow!("invalid target configuration: .concurro/config.yaml")
            })? {
                ensure!(
                    !matches!(token.1, TokenType::Alias(_)),
                    "YAML aliases are not supported in target configuration"
                );
            }
            serde_yaml_ng::from_str::<StrictValue>(text).map_err(|_| {
                anyhow::anyhow!("invalid target configuration: .concurro/config.yaml")
            })?;
            serde_yaml_ng::from_str::<Value>(text).map_err(|_| {
                anyhow::anyhow!("invalid target configuration: .concurro/config.yaml")
            })?
        }
        _ => bail!("provide exactly one target configuration file"),
    };
    let object = raw
        .as_object()
        .ok_or_else(|| anyhow::anyhow!("target configuration must be an object"))?;
    let string = |key: &str| -> Result<String> {
        Ok(object
            .get(key)
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow::anyhow!("target configuration requires {key}"))?
            .to_owned())
    };
    let count = |key: &str, minimum: u64| -> Result<u64> {
        let value = object
            .get(key)
            .and_then(Value::as_u64)
            .ok_or_else(|| anyhow::anyhow!("target configuration requires {key}"))?;
        ensure!(value >= minimum, "target configuration has invalid {key}");
        Ok(value)
    };
    let repository = string("repository")?;
    let base = string("base")?;
    let configured_bot = string("bot_login")?;
    ensure!(
        repository.eq_ignore_ascii_case(fork),
        "target configuration names another repository"
    );
    ensure!(
        configured_bot == bot_login,
        "target bot_login differs from the App"
    );
    ensure!(
        object
            .get("engine")
            .is_none_or(|value| value.as_str() == Some(engine_sha)),
        "target pins another engine commit"
    );
    ensure!(
        !base.is_empty() && !base.contains("..") && !base.starts_with(['/', '-']),
        "target base branch is invalid"
    );
    ensure!(
        base.len() <= 100
            && base
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"._/-".contains(&byte)),
        "target base branch is invalid"
    );
    ensure!(
        object.get("paths").and_then(Value::as_object).is_some(),
        "target paths policy is invalid"
    );
    let paths = object["paths"].as_object().expect("checked paths map");
    let legacy = paths.contains_key("editable_extensions");
    let permitted = [
        "protected_names",
        "protected_name_prefixes",
        "protected_prefixes",
        "protected_files",
        "protected",
        "generated",
    ];
    let path_keys = paths.keys().map(String::as_str).collect::<BTreeSet<_>>();
    let required = permitted.into_iter().collect::<BTreeSet<_>>();
    ensure!(
        if legacy {
            path_keys
                == required
                    .union(&["editable_extensions"].into())
                    .copied()
                    .collect()
        } else {
            path_keys.contains("protected_extensions")
                && path_keys
                    .difference(&required)
                    .all(|key| *key == "protected_extensions")
        },
        "target paths policy is invalid"
    );
    for (key, value) in paths {
        let entries = value
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("target paths.{key} is invalid"))?;
        let strings = entries
            .iter()
            .map(|entry| {
                entry
                    .as_str()
                    .filter(|text| !text.trim().is_empty())
                    .ok_or_else(|| anyhow::anyhow!("target paths.{key} is invalid"))
            })
            .collect::<Result<Vec<_>>>()?;
        ensure!(
            strings.iter().collect::<BTreeSet<_>>().len() == strings.len(),
            "target paths.{key} is duplicated"
        );
        if key == "editable_extensions" || key == "protected_extensions" {
            ensure!(
                (key != "editable_extensions" || !strings.is_empty())
                    && strings
                        .iter()
                        .all(|text| text.starts_with('.') && text.len() > 1),
                "target paths.{key} is invalid"
            );
        }
        if key == "protected_prefixes" {
            ensure!(
                strings.iter().all(|text| text.ends_with('/')),
                "target paths.{key} is invalid"
            );
        }
        if key == "protected" || key == "generated" {
            ensure!(
                strings.iter().all(|text| {
                    !text.starts_with('/')
                        && !text.contains('\\')
                        && text
                            .split('/')
                            .all(|part| !part.is_empty() && part != "." && part != "..")
                }),
                "target paths.{key} is invalid"
            );
        }
    }
    if let Some(mcp) = object.get("mcp") {
        let mcp = mcp
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("target mcp is invalid"))?;
        ensure!(mcp.len() == 1, "target mcp is invalid");
        let servers = mcp
            .get("servers")
            .and_then(Value::as_object)
            .ok_or_else(|| anyhow::anyhow!("target mcp is invalid"))?;
        for (name, value) in servers {
            ensure!(
                name.bytes()
                    .next()
                    .is_some_and(|first| first.is_ascii_alphanumeric())
                    && name
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte)),
                "target MCP server name is invalid"
            );
            let server = value
                .as_object()
                .ok_or_else(|| anyhow::anyhow!("target MCP server {name} is invalid"))?;
            ensure!(
                server.len() >= 2
                    && server.keys().all(|key| matches!(
                        key.as_str(),
                        "transport" | "command" | "args" | "env"
                    ))
                    && server.get("transport").and_then(Value::as_str) == Some("stdio")
                    && server
                        .get("command")
                        .and_then(Value::as_str)
                        .is_some_and(|text| !text.trim().is_empty())
                    && server.get("args").is_none_or(|args| args
                        .as_array()
                        .is_some_and(|items| items.iter().all(Value::is_string)))
                    && server
                        .get("env")
                        .is_none_or(|env| env.as_object().is_some_and(|items| items.iter().all(
                            |(key, value)| {
                                key.bytes().next().is_some_and(|first| {
                                    first.is_ascii_alphabetic() || first == b'_'
                                }) && key
                                    .bytes()
                                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
                                    && value.is_string()
                            }
                        ))),
                "target MCP server {name} is invalid"
            );
        }
    }
    if let Some(retained) = object.get("max_retained_bytes") {
        ensure!(
            retained.is_null() || retained.as_u64().is_some_and(|bytes| bytes > 0),
            "target max_retained_bytes is invalid"
        );
    }
    ensure!(
        object
            .get("max_context_bytes")
            .and_then(Value::as_u64)
            .is_some_and(|bytes| bytes > 0),
        "target max_context_bytes is invalid"
    );
    let policy_documents = object
        .get("policy_documents")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow::anyhow!("target configuration requires policy_documents"))?
        .iter()
        .map(|value| {
            value
                .as_str()
                .filter(|path| {
                    !path.is_empty()
                        && !path.starts_with('/')
                        && !path.split('/').any(|part| part == "..")
                })
                .map(str::to_owned)
                .ok_or_else(|| anyhow::anyhow!("target policy document path is invalid"))
        })
        .collect::<Result<Vec<_>>>()?;
    ensure!(
        !policy_documents.is_empty(),
        "target policy documents are empty"
    );
    ensure!(
        policy_documents.iter().collect::<BTreeSet<_>>().len() == policy_documents.len(),
        "target policy documents are duplicated"
    );
    Ok(TargetConfig {
        repository,
        base,
        bot_login: configured_bot,
        max_model_calls: count("max_model_calls", 1)?,
        max_fix_rounds: count("max_fix_rounds", 0)?,
        max_plan_revisions: count("max_plan_revisions", 0)?,
        policy_documents,
        raw,
    })
}

#[cfg(test)]
mod tests {
    use super::decode_target_config;
    use serde_json::Value;

    #[test]
    fn one_unambiguous_target_configuration_is_required() {
        let config = include_str!("../../../engine/tests/fixtures/legacy-config.json");
        let decoded = decode_target_config(
            Some(config),
            None,
            "cylewitruk/stacks-core-autobot",
            "concurro[bot]",
            "engine",
        )
        .expect("legacy target");
        assert_eq!(decoded.max_model_calls, 48);
        assert_eq!(decoded.policy_documents.len(), 3);
        assert!(
            decode_target_config(
                Some(config),
                Some(config),
                "cylewitruk/stacks-core-autobot",
                "concurro[bot]",
                "engine"
            )
            .is_err()
        );
        assert!(
            decode_target_config(
                Some("{\"base\":\"a\",\"base\":\"b\"}"),
                None,
                "fork",
                "bot",
                "engine"
            )
            .is_err()
        );
        assert!(
            decode_target_config(
                None,
                Some("repository: fork\nrepository: other\n"),
                "fork",
                "bot",
                "engine"
            )
            .is_err()
        );
        let yaml = "repository: fork\nbase: main\nbot_login: bot\nmax_model_calls: 1\nmax_fix_rounds: 0\nmax_plan_revisions: 0\nmax_context_bytes: 350000\npolicy_documents: [README.md]\npaths: {protected_extensions: []}\n";
        assert!(decode_target_config(None, Some(yaml), "fork", "bot", "engine").is_ok());
        let with_mcp = format!(
            "{yaml}mcp: {{servers: {{tool: {{transport: stdio, command: tool, args: [\"--root={{workspace}}\"], env: {{RA_LOG: error}}}}}}}}\nmax_retained_bytes: null\n"
        );
        decode_target_config(None, Some(&with_mcp), "fork", "bot", "engine")
            .expect("valid MCP and null retained limit");
        assert!(
            decode_target_config(
                None,
                Some(&with_mcp.replace("transport: stdio", "transport: http")),
                "fork",
                "bot",
                "engine"
            )
            .is_err()
        );
        assert!(
            decode_target_config(
                None,
                Some(&with_mcp.replace("RA_LOG", "RA-LOG")),
                "fork",
                "bot",
                "engine"
            )
            .is_err()
        );
        let aliased = yaml.replace(
            "policy_documents: [README.md]",
            "policy_documents: &docs [README.md]\nother: *docs",
        );
        assert!(decode_target_config(None, Some(&aliased), "fork", "bot", "engine").is_err());
    }

    #[test]
    fn json_and_yaml_numbers_keep_their_scalar_values() {
        let config = include_str!("../../../engine/tests/fixtures/legacy-config.json");
        let mut value: Value = serde_json::from_str(config).expect("fixture");
        let fields = value.as_object_mut().expect("config object");
        fields.insert(
            "fractional".to_owned(),
            serde_json::from_str("1.5").expect("float"),
        );
        fields.insert(
            "large".to_owned(),
            serde_json::from_str("18446744073709551616").expect("integer"),
        );
        let json = serde_json::to_string(&value).expect("JSON config");
        let decoded = decode_target_config(
            Some(&json),
            None,
            "cylewitruk/stacks-core-autobot",
            "concurro[bot]",
            "engine",
        )
        .expect("JSON numbers");
        assert_eq!(decoded.raw["fractional"].to_string(), "1.5");
        assert_eq!(decoded.raw["large"].to_string(), "18446744073709551616");
        assert!(decoded.raw["fractional"].is_number());
        assert!(decoded.raw["large"].is_number());

        let yaml = "repository: fork\nbase: main\nbot_login: bot\nmax_model_calls: 1\nmax_fix_rounds: 0\nmax_plan_revisions: 0\nmax_context_bytes: 350000\npolicy_documents: [README.md]\npaths: {protected_extensions: []}\nfractional: 1.5\n";
        let decoded =
            decode_target_config(None, Some(yaml), "fork", "bot", "engine").expect("YAML numbers");
        assert_eq!(decoded.raw["fractional"].to_string(), "1.5");
        assert!(decoded.raw["fractional"].is_number());
    }
}
