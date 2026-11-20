//! M1 issue comment text derived from pinned task state and intake evidence.

use serde_json::Value;

use crate::{IntakeReport, Stage, TaskState};

/// Render untrusted source text as one bounded Markdown-safe line.
pub(crate) fn markdown_text(value: &str, limit: usize) -> String {
    let line = value.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut escaped = String::new();
    for character in line.chars() {
        let before = escaped.len();
        match character {
            '\0' => escaped.push_str("\\u0000"),
            character if character.is_control() => continue,
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '\\' | '`' | '*' | '_' | '[' | ']' | '(' | ')' | '!' | '~' | '|' => {
                escaped.push('\\');
                escaped.push(character);
            }
            _ => escaped.push(character),
        }
        if escaped.len() > limit.saturating_sub(3) {
            escaped.truncate(before);
            escaped.push('…');
            break;
        }
    }
    escaped
}

/// Mention a GitHub login only when it has the platform's plain login syntax.
fn github_mention(login: &str) -> String {
    if !login.is_empty()
        && login.len() <= 39
        && login.starts_with(|character: char| character.is_ascii_alphanumeric())
        && login
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || character == '-')
    {
        format!("@{login} ")
    } else {
        String::new()
    }
}

/// Introduce the pinned team once after initialization succeeds.
pub fn welcome_body(configuration: &Value, actor: &str) -> String {
    let agents = configuration.get("agents");
    let roles = [
        ("Intake", "triage", "qc"),
        ("Planning", "planner", "planner"),
        ("Design review", "plan_reviewers", "reviewers"),
        ("Implementation", "implementer", "implementer"),
        ("PR review", "pr_reviewers", "reviewers"),
    ];
    let mut lines = vec![
        format!(
            "{}I've picked up this issue and will start digging in!",
            github_mention(actor)
        ),
        String::new(),
        "Here's the team which has currently been assigned:".to_owned(),
        String::new(),
        "| Role | Model (effort) |".to_owned(),
        "| ---- | ---- |".to_owned(),
    ];
    for (role, named, legacy) in roles {
        let assignment = agents
            .and_then(|agents| agents.get(named))
            .or_else(|| configuration.get(legacy));
        let names: Vec<&str> = match assignment {
            Some(Value::String(name)) => vec![name],
            Some(Value::Array(names)) => names.iter().filter_map(Value::as_str).collect(),
            _ => Vec::new(),
        };
        let models = names
            .into_iter()
            .map(|name| {
                let profile = &configuration["models"][name];
                let model = if matches!(role, "Intake" | "Design review") && agents.is_none() {
                    configuration["qc_model"]
                        .as_str()
                        .filter(|value| !value.is_empty())
                        .or_else(|| profile.as_str())
                } else {
                    profile.as_str().or_else(|| profile["model"].as_str())
                }
                .filter(|value| !value.is_empty())
                .unwrap_or("CLI default");
                let effort = profile["effort"].as_str().unwrap_or_else(|| match name {
                    "claude" => "medium",
                    "codex" => "low",
                    _ => "CLI default",
                });
                format!(
                    "{} ({})",
                    markdown_text(model, 200),
                    markdown_text(effort, 100)
                )
            })
            .collect::<Vec<_>>()
            .join(" · ");
        lines.push(format!("| {role} | {models} |"));
    }
    lines.join("\n")
}

/// Render the current intake disposition as one immutable issue notice.
pub fn intake_notice_body(state: &TaskState, report: Option<&IntakeReport>) -> Option<String> {
    match state.stage {
        Stage::Plan => {
            let report = report?;
            if report.concerns.is_empty() {
                return None;
            }
            let concerns = report
                .concerns
                .iter()
                .take(20)
                .map(|concern| format!("- {}", markdown_text(concern, 1000)))
                .collect::<Vec<_>>()
                .join("\n");
            Some(format!(
                "### Intake approved with concerns\n\nPlanning will continue. No action needed.\n\n<details><summary>Planning concerns</summary>\n\n{concerns}\n\nThe full concerns are retained in the intake report.\n\n</details>"
            ))
        }
        Stage::Clarify => {
            let report = report?;
            let mention = github_mention(&state.author.login);
            let mention = if mention.is_empty() {
                mention
            } else {
                format!("{}, ", mention.trim_end())
            };
            let questions = report
                .questions
                .iter()
                .enumerate()
                .map(|(index, question)| {
                    format!("**Q{}.** {}", index + 1, markdown_text(question, 1000))
                })
                .take(20)
                .collect::<Vec<_>>()
                .join("\n\n");
            Some(format!(
                "### ❓ Input needed\n\n{mention}Concurro needs answers before it can continue.\n\n{questions}\n\nReply in a comment or edit the issue description; the author or any writer may answer. Then a writer comments `/concurro start`."
            ))
        }
        Stage::Rejected => Some(format!(
            "### ⛔ Rejected\n\n{}",
            markdown_text(
                state
                    .stage_reason
                    .as_deref()
                    .unwrap_or("Intake rejected this issue."),
                1000
            )
        )),
        Stage::Escalated => Some(format!(
            "### ⚠️ Escalated\n\n{}",
            markdown_text(
                state
                    .stage_reason
                    .as_deref()
                    .unwrap_or("The task needs human attention."),
                1000
            )
        )),
        Stage::Blocked => Some(format!(
            "### ⛔ Blocked\n\n{}",
            markdown_text(
                state
                    .stage_reason
                    .as_deref()
                    .unwrap_or("The task needs attention before continuing."),
                1000
            )
        )),
        _ => None,
    }
}
