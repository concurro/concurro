//! Cross-language contract fixture checks.

use std::{assert_matches, fs, path::Path};

use concurro_contracts::{EventId, GithubIssues, REGISTRY, decode};
use serde_json::Value;

/// Read and round-trip every valid GitHub issue event fixture.
#[test]
fn valid_github_issues_round_trip() {
    for registration in REGISTRY {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!(
            "../../contracts/fixtures/{}.v{}",
            registration.family, registration.version
        ));
        for entry in fs::read_dir(root.join("valid")).expect("valid fixtures") {
            let path = entry.expect("fixture entry").path();
            let raw = fs::read(&path).expect("fixture bytes");
            let actual = (registration.round_trip)(&raw).expect("valid event");
            let expected = root
                .join("expected")
                .join(path.file_name().expect("fixture name"));
            let expected_bytes = fs::read(&expected).unwrap_or(raw);
            let expected: Value = serde_json::from_slice(&expected_bytes).expect("expected JSON");
            assert_eq!(actual, expected, "{}", path.display());
        }
    }
}

/// Reject malformed values and unknown members of closed enums.
#[test]
fn invalid_github_issues_fail() {
    for registration in REGISTRY {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!(
            "../../contracts/fixtures/{}.v{}/invalid",
            registration.family, registration.version
        ));
        for entry in fs::read_dir(root).expect("invalid fixtures") {
            let path = entry.expect("fixture entry").path();
            let raw = fs::read(&path).expect("fixture bytes");
            assert!(
                (registration.round_trip)(&raw).is_err(),
                "{}",
                path.display()
            );
        }
    }
}

/// A future version is rejected before its payload is interpreted.
#[test]
fn version_is_read_before_data() {
    let raw = br#"{"type":"github.issues.labeled","version":2,"data":{"unreadable":true}}"#;
    assert_matches!(
        decode::<GithubIssues>(raw),
        Err(concurro_contracts::ContractError::UnsupportedVersion { found: 2, .. })
    );
}

/// Pin the durable GitHub event ID derivation across producer releases.
#[test]
fn github_event_id_golden() {
    assert_eq!(
        EventId::github("delivery-1", 1).0.to_string(),
        "669a0c7f-922d-5b0c-a579-4e19e2807e31"
    );
    assert_ne!(
        EventId::github("delivery-1", 1),
        EventId::github("delivery-1", 2)
    );
}
