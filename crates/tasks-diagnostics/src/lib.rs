//! Decode original runner archives into searchable, position-preserving events.

use std::io::{Cursor, Read};

use base64::{Engine, engine::general_purpose::STANDARD};
pub use concurro_jsonb::jsonb_representable;
use flate2::read::GzDecoder;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tar::Archive;

/// One complete original gzip and the independently decoded transcript snapshot.
pub struct ArchiveSnapshot {
    /// SHA-256 of the original gzip bytes, used as the object key.
    pub gzip_sha256: String,
    /// Original bytes retained until object readback verifies them.
    pub gzip_bytes: Vec<u8>,
    /// SHA-256 of the uncompressed tar stream, shared across gzip encodings.
    pub tar_sha256: Option<String>,
    /// Total uncompressed regular-file bytes available to a restored session.
    pub retained_file_bytes: Option<u64>,
    /// Ordered source records, empty if the archive could not be read.
    pub events: Vec<TranscriptEvent>,
    /// Root continuation journal when present and valid JSON.
    pub journal: Option<Value>,
    /// Fixed decoding stage for an unreadable archive.
    pub error: Option<&'static str>,
}

/// One JSONL line with its original source position and full searchable text.
pub struct TranscriptEvent {
    /// Tar member path, interpreted as UTF-8 where possible.
    pub member_path: String,
    /// Tar member modification time.
    pub member_mtime: u64,
    /// Original archive entry ordinal.
    pub member_ordinal: usize,
    /// One-based JSONL line number.
    pub line_number: usize,
    /// Agent family inferred from the transcript path.
    pub harness: &'static str,
    /// Event kind when the line is a JSON object with a type.
    pub kind: Option<String>,
    /// JSON value only when PostgreSQL jsonb can represent it.
    pub json: Option<Value>,
    /// Exact source line bytes for malformed or nonrepresentable JSON.
    pub fallback_bytes: Option<Vec<u8>>,
    /// Full decodable text, including tool names, arguments and results.
    pub search_text: String,
    /// Diagnostic for a fallback source line.
    pub diagnostic: Option<&'static str>,
}

impl ArchiveSnapshot {
    /// Decode a runner's base64 archive while preserving valid gzip bytes on tar failure.
    pub fn from_base64(encoded: &str) -> Result<Self, &'static str> {
        let bytes = STANDARD.decode(encoded).map_err(|_| "invalid_base64")?;
        Ok(Self::from_bytes(bytes))
    }

    /// Decode the original gzip without changing the bytes that will be uploaded.
    pub fn from_bytes(gzip_bytes: Vec<u8>) -> Self {
        let gzip_sha256 = hex::encode(Sha256::digest(&gzip_bytes));
        let mut snapshot = Self {
            gzip_sha256,
            gzip_bytes,
            tar_sha256: None,
            retained_file_bytes: None,
            events: Vec::new(),
            journal: None,
            error: None,
        };
        let mut tar_bytes = Vec::new();
        if GzDecoder::new(snapshot.gzip_bytes.as_slice())
            .read_to_end(&mut tar_bytes)
            .is_err()
        {
            snapshot.error = Some("invalid_gzip");
            return snapshot;
        }
        match parse_tar(&tar_bytes) {
            Ok((events, journal, retained_file_bytes)) => {
                snapshot.tar_sha256 = Some(hex::encode(Sha256::digest(&tar_bytes)));
                snapshot.retained_file_bytes = Some(retained_file_bytes);
                snapshot.events = events;
                snapshot.journal = journal;
            }
            Err(stage) => snapshot.error = Some(stage),
        }
        snapshot
    }
}

/// Read every regular JSONL member and the root continuation journal.
fn parse_tar(bytes: &[u8]) -> Result<(Vec<TranscriptEvent>, Option<Value>, u64), &'static str> {
    let mut archive = Archive::new(Cursor::new(bytes));
    let entries = archive.entries().map_err(|_| "invalid_tar")?;
    let mut events = Vec::new();
    let mut journal = None;
    let mut retained_file_bytes = 0_u64;
    for (ordinal, entry) in entries.enumerate() {
        let mut entry = entry.map_err(|_| "invalid_tar")?;
        if !entry.header().entry_type().is_file() {
            continue;
        }
        retained_file_bytes = retained_file_bytes
            .checked_add(entry.header().size().map_err(|_| "invalid_tar_size")?)
            .ok_or("archive_size_overflow")?;
        i64::try_from(retained_file_bytes).map_err(|_| "archive_size_overflow")?;
        let path = entry
            .path()
            .map_err(|_| "invalid_tar_path")?
            .to_string_lossy()
            .replace('\0', "\\u0000");
        i32::try_from(ordinal).map_err(|_| "archive_position_overflow")?;
        if path != "turn.json" && !path.ends_with(".jsonl") {
            continue;
        }
        let mut content = Vec::new();
        entry
            .read_to_end(&mut content)
            .map_err(|_| "invalid_tar_member")?;
        if path == "turn.json" {
            journal = serde_json::from_slice(&content)
                .ok()
                .filter(jsonb_representable);
            continue;
        }
        let mtime = entry.header().mtime().map_err(|_| "invalid_tar_mtime")?;
        i64::try_from(mtime).map_err(|_| "invalid_tar_mtime")?;
        let harness = if path.starts_with("codex/") {
            "codex"
        } else {
            "claude"
        };
        for (line, raw) in content.split_inclusive(|byte| *byte == b'\n').enumerate() {
            i32::try_from(line + 1).map_err(|_| "archive_position_overflow")?;
            if raw.is_empty() {
                continue;
            }
            let parsed = serde_json::from_slice::<Value>(raw);
            let (json, fallback_bytes, diagnostic, search_text, kind) = match parsed {
                Ok(value) => {
                    let kind = value
                        .get("type")
                        .and_then(Value::as_str)
                        .map(|text| text.replace('\0', "\\u0000"));
                    let search_text = event_text(&value);
                    if jsonb_representable(&value) {
                        (Some(value), None, None, search_text, kind)
                    } else {
                        (
                            None,
                            Some(raw.to_vec()),
                            Some("jsonb_unrepresentable"),
                            search_text,
                            kind,
                        )
                    }
                }
                Err(_) => (
                    None,
                    Some(raw.to_vec()),
                    Some("invalid_json"),
                    fallback_text(raw),
                    None,
                ),
            };
            events.push(TranscriptEvent {
                member_path: path.clone(),
                member_mtime: mtime,
                member_ordinal: ordinal,
                line_number: line + 1,
                harness,
                kind,
                json,
                fallback_bytes,
                search_text,
                diagnostic,
            });
        }
    }
    events.sort_by(|a, b| {
        (
            a.member_mtime,
            &a.member_path,
            a.member_ordinal,
            a.line_number,
        )
            .cmp(&(
                b.member_mtime,
                &b.member_path,
                b.member_ordinal,
                b.line_number,
            ))
    });
    Ok((events, journal, retained_file_bytes))
}

/// Extract searchable JSON text in source order for full-text indexing.
fn event_text(value: &Value) -> String {
    let mut parts = Vec::new();
    collect_text(value, &mut parts);
    parts.join("\n")
}

/// Decode valid JSON escapes while separating lone surrogates in fallback JSONL.
fn fallback_text(raw: &[u8]) -> String {
    let text = String::from_utf8_lossy(raw).replace('\0', " \\u0000 ");
    let bytes = text.as_bytes();
    let mut repaired = String::new();
    let mut start = 0;
    let mut index = 0;
    while index + 6 <= bytes.len() {
        if bytes[index] == b'\\'
            && bytes[index + 1] == b'u'
            && bytes[..index]
                .iter()
                .rev()
                .take_while(|byte| **byte == b'\\')
                .count()
                % 2
                == 0
            && let Some(codepoint) = std::str::from_utf8(&bytes[index + 2..index + 6])
                .ok()
                .and_then(|hex| u16::from_str_radix(hex, 16).ok())
            && (0xD800..=0xDFFF).contains(&codepoint)
        {
            let paired = (0xD800..=0xDBFF).contains(&codepoint)
                && index + 12 <= bytes.len()
                && bytes[index + 6..index + 8] == *b"\\u"
                && std::str::from_utf8(&bytes[index + 8..index + 12])
                    .ok()
                    .and_then(|hex| u16::from_str_radix(hex, 16).ok())
                    .is_some_and(|low| (0xDC00..=0xDFFF).contains(&low));
            if paired {
                index += 12;
                continue;
            }
            repaired.push_str(&text[start..index]);
            repaired.push_str(" \\\\u");
            repaired.push_str(&text[index + 2..index + 6]);
            repaired.push(' ');
            index += 6;
            start = index;
        } else {
            index += 1;
        }
    }
    repaired.push_str(&text[start..]);
    serde_json::from_str::<Value>(&repaired)
        .map(|value| event_text(&value))
        .unwrap_or(repaired)
}

/// Preserve field names, values, tool inputs and outputs without clipping.
fn collect_text(value: &Value, parts: &mut Vec<String>) {
    match value {
        Value::String(text) if !text.is_empty() => parts.push(text.replace('\0', " \\u0000 ")),
        Value::Number(number) => parts.push(number.to_string()),
        Value::Bool(boolean) => parts.push(boolean.to_string()),
        Value::Array(values) => {
            for value in values {
                collect_text(value, parts);
            }
        }
        Value::Object(fields) => {
            for (key, value) in fields {
                parts.push(key.replace('\0', " \\u0000 "));
                collect_text(value, parts);
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use flate2::{Compression, write::GzEncoder};
    use tar::{Builder, Header};

    use super::{ArchiveSnapshot, fallback_text, jsonb_representable};

    #[test]
    fn out_of_range_jsonb_numbers_keep_their_source_fallback() {
        let large_integer: serde_json::Value =
            serde_json::from_str("18446744073709551616").expect("integer");
        assert!(jsonb_representable(&large_integer));
        assert_eq!(large_integer.to_string(), "18446744073709551616");
        let huge_exponent: serde_json::Value =
            serde_json::from_str("1e131072").expect("JSON number");
        assert!(!jsonb_representable(&huge_exponent));
        let tiny_exponent: serde_json::Value =
            serde_json::from_str("1e-16384").expect("JSON number");
        assert!(!jsonb_representable(&tiny_exponent));
        assert!(fallback_text(b"{\"text\":\"left\\ud800right\"}").contains("left \\ud800 right"));
        let fallback = fallback_text(
            br#"{"bad":"left\ud800right","text":"\nneedle\u0061lpha","pair":"\ud83d\ude00"}"#,
        );
        assert!(fallback.contains("needlealpha"));
        assert!(fallback.contains("😀"));
        assert!(fallback.contains("left \\ud800 right"));
    }

    #[test]
    fn original_gzip_and_all_source_lines_survive_jsonb_fallback() {
        let mut tar = Builder::new(Vec::new());
        for (path, mtime, content) in [
            ("claude/z.jsonl", 20, b"{\"type\":\"assistant\",\"message\":{\"content\":\"late\"}}\n".as_slice()),
            ("codex/a.jsonl", 10, b"{\"type\":\"response_item\",\"payload\":{\"name\":\"shell\",\"arguments\":{\"amount\":18446744073709551616,\"enabled\":true,\"needle\":\"alpha\"}}}\n{\"type\":\"mes\\u0000sage\",\"text\":\"bad\\u0000word\"}\n{broken\n".as_slice()),
        ] {
            let mut header = Header::new_gnu();
            header.set_size(content.len() as u64);
            header.set_mtime(mtime);
            header.set_mode(0o644);
            header.set_cksum();
            tar.append_data(&mut header, path, content).expect("member");
        }
        let tar_bytes = tar.into_inner().expect("tar");
        let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(&tar_bytes).expect("gzip write");
        let gzip = encoder.finish().expect("gzip finish");
        let parsed = ArchiveSnapshot::from_bytes(gzip.clone());
        assert_eq!(parsed.gzip_bytes, gzip);
        assert!(parsed.error.is_none());
        assert_eq!(parsed.events.len(), 4);
        assert_eq!(parsed.events[0].member_path, "codex/a.jsonl");
        assert!(parsed.events[0].search_text.contains("shell"));
        assert!(parsed.events[0].search_text.contains("alpha"));
        assert!(
            parsed.events[0]
                .search_text
                .contains("18446744073709551616")
        );
        assert!(parsed.events[0].search_text.contains("enabled\ntrue"));
        assert!(
            parsed.events[0].search_text.find("amount").unwrap()
                < parsed.events[0].search_text.find("enabled").unwrap()
        );
        assert!(
            parsed.events[0]
                .json
                .as_ref()
                .unwrap()
                .to_string()
                .contains("18446744073709551616")
        );
        assert_eq!(parsed.events[1].diagnostic, Some("jsonb_unrepresentable"));
        assert_eq!(parsed.events[1].kind.as_deref(), Some("mes\\u0000sage"));
        assert!(parsed.events[1].json.is_none());
        assert_eq!(
            parsed.events[1].fallback_bytes.as_deref(),
            Some(b"{\"type\":\"mes\\u0000sage\",\"text\":\"bad\\u0000word\"}\n".as_slice())
        );
        assert!(parsed.events[1].search_text.contains("bad \\u0000 word"));
        assert_eq!(parsed.events[2].diagnostic, Some("invalid_json"));
        assert_eq!(parsed.events[3].member_path, "claude/z.jsonl");
        assert!(parsed.tar_sha256.is_some());
    }

    #[test]
    fn unreadable_archive_keeps_original_gzip_for_inspection() {
        let bytes = b"not gzip".to_vec();
        let parsed = ArchiveSnapshot::from_bytes(bytes.clone());
        assert_eq!(parsed.gzip_bytes, bytes);
        assert_eq!(parsed.error, Some("invalid_gzip"));
        assert!(parsed.tar_sha256.is_none());
        assert!(parsed.events.is_empty());
        assert!(ArchiveSnapshot::from_base64("??").is_err());
    }
}
