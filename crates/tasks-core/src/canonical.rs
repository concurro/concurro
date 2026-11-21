//! Canonical JSON identity compatible with the retained Python controller.

use anyhow::Result;
use serde_json::Value;
use sha2::{Digest, Sha256};

/// Hash JSON with Python's sorted-key, ASCII-escaped `json.dumps` encoding.
pub fn python_digest(value: &Value) -> Result<String> {
    let encoded = python_json(value)?;
    Ok(hex::encode(Sha256::digest(encoded.as_bytes())))
}

/// Serialize a value with Python's sorted-key, ASCII-escaped default JSON format.
pub fn python_json(value: &Value) -> Result<String> {
    let mut encoded = String::new();
    write_python_json(value, &mut encoded)?;
    Ok(encoded)
}

/// Escape non-ASCII JSON characters with Python's surrogate-pair convention.
pub fn escape_non_ascii(value: &str) -> String {
    let mut output = String::new();
    for character in value.chars() {
        if character.is_ascii() {
            output.push(character);
        } else {
            let codepoint = u32::from(character);
            if codepoint <= 0xffff {
                output.push_str(&format!("\\u{codepoint:04x}"));
            } else {
                let adjusted = codepoint - 0x10000;
                output.push_str(&format!(
                    "\\u{:04x}\\u{:04x}",
                    0xd800 + (adjusted >> 10),
                    0xdc00 + (adjusted & 0x3ff)
                ));
            }
        }
    }
    output
}

/// Append one JSON value in the legacy digest's canonical byte format.
fn write_python_json(value: &Value, output: &mut String) -> Result<()> {
    match value {
        Value::Null => output.push_str("null"),
        Value::Bool(value) => output.push_str(if *value { "true" } else { "false" }),
        Value::Number(value) => output.push_str(&value.to_string()),
        Value::String(value) => {
            output.push_str(&escape_non_ascii(&serde_json::to_string(value)?));
        }
        Value::Array(values) => {
            output.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    output.push_str(", ");
                }
                write_python_json(value, output)?;
            }
            output.push(']');
        }
        Value::Object(values) => {
            output.push('{');
            let mut fields = values.iter().collect::<Vec<_>>();
            fields.sort_by(|(left, _), (right, _)| left.cmp(right));
            for (index, (key, value)) in fields.into_iter().enumerate() {
                if index > 0 {
                    output.push_str(", ");
                }
                write_python_json(&Value::String(key.clone()), output)?;
                output.push_str(": ");
                write_python_json(value, output)?;
            }
            output.push('}');
        }
    }
    Ok(())
}
