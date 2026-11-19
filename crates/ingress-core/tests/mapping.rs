//! Pinned GitHub payloads must keep mapping to the same revision and wire event.

use std::{fs, path::PathBuf};

use chrono::{TimeZone, Utc};
use concurro_ingress_core::{MappedEvent, Mapping, map_delivery};
use serde_json::Value;

/// Every modeled family maps its captured payload to the committed event fixture.
#[test]
fn maps_every_github_family() {
    let families = [
        "issues",
        "issue_comment",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "pull_request_review_thread",
        "push",
        "status",
        "check_run",
        "check_suite",
        "workflow_run",
    ];
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../contracts/fixtures");
    let received = Utc.with_ymd_and_hms(2026, 11, 19, 10, 1, 0).unwrap();
    for family in families {
        let folder = root.join(format!("github.{family}.v1"));
        for entry in fs::read_dir(folder.join("raw")).expect("raw fixtures") {
            let path = entry.expect("raw fixture").path();
            let name = path.file_name().expect("fixture name");
            let raw = fs::read(&path).expect("raw payload");
            let expected: Value = serde_json::from_slice(
                &fs::read(folder.join("expected").join(name)).expect("expected event"),
            )
            .expect("expected JSON");
            let mapped = map_delivery("delivery", family, &raw, received, &[7]);
            let Mapping::Published(_, event) = mapped else {
                panic!("{family} {name:?} failed to map: {mapped:?}");
            };
            let actual = match *event {
                MappedEvent::Issues(event) => serde_json::to_value(event),
                MappedEvent::IssueComment(event) => serde_json::to_value(event),
                MappedEvent::PullRequest(event) => serde_json::to_value(event),
                MappedEvent::PullRequestReview(event) => serde_json::to_value(event),
                MappedEvent::PullRequestReviewComment(event) => serde_json::to_value(event),
                MappedEvent::PullRequestReviewThread(event) => serde_json::to_value(event),
                MappedEvent::Push(event) => serde_json::to_value(event),
                MappedEvent::Status(event) => serde_json::to_value(event),
                MappedEvent::CheckRun(event) => serde_json::to_value(event),
                MappedEvent::CheckSuite(event) => serde_json::to_value(event),
                MappedEvent::WorkflowRun(event) => serde_json::to_value(event),
            }
            .expect("event JSON");
            assert_eq!(actual, expected, "{family} {name:?}");
        }
    }
}
