//! Isolated agy process invocation and role-scoped conversation retention.

use std::{
    collections::BTreeMap,
    env, fs,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OpenFlags, backup::Backup};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::{
    io::{self, AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::Command,
    time,
};
use uuid::Uuid;

use crate::validate_model;

/// One already-bound invocation supplied by the retained runner bridge.
#[derive(Deserialize, Serialize)]
pub struct Invocation {
    /// Canonical Gemini model ID.
    pub model: String,
    /// Explicit model-specific reasoning effort.
    pub effort: String,
    /// Task checkout used by file tools and shell commands.
    pub workspace: PathBuf,
    /// Private disposable files for this turn.
    pub scratch: PathBuf,
    /// Root of the selected role's retained archive, when enabled.
    pub retained_root: Option<PathBuf>,
    /// Existing conversation UUID, only for a continuation.
    pub resume_id: Option<Uuid>,
    /// Gateway URL; only the derived session key authenticates this caller.
    pub gateway_url: String,
    /// Native structured-output schema path.
    pub schema_path: PathBuf,
    /// One user event containing the bound stage prompt.
    pub prompt: String,
    /// Optional stage execution timeout; runner-bound stages use the outer job lifetime.
    pub timeout: Option<u64>,
    /// Resolved target MCP servers, recreated independently for every job.
    #[serde(default)]
    pub mcp_servers: BTreeMap<String, Value>,
}

/// Successful CLI result passed back to the existing stage validation and journal.
#[derive(Serialize)]
pub struct Completed {
    /// Object returned through agy's native final-result schema.
    pub structured_output: Value,
    /// Provider-local conversation UUID retained for the next role turn.
    pub conversation_id: Uuid,
}

/// Nondefinitive process outcomes retain the outer runner's retry semantics.
#[derive(Debug)]
pub enum Interrupted {
    /// Stage execution deadline expired.
    Timeout,
    /// The provider process was terminated by a signal.
    Signal,
}

impl std::fmt::Display for Interrupted {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Timeout => "agy invocation timed out",
            Self::Signal => "agy process was interrupted",
        })
    }
}
impl std::error::Error for Interrupted {}

/// Keep only one completed native result while streaming away nonterminal events.
#[derive(Default)]
struct ResultReader {
    completed: Option<Completed>,
}

impl ResultReader {
    /// Validate the CLI's terminal event without accepting text as structured output.
    fn event(&mut self, event: Value) -> Result<()> {
        if event["event"] != "result" {
            return Ok(());
        }
        ensure!(self.completed.is_none(), "duplicate agy terminal result");
        let result = &event["result"];
        ensure!(
            result["status"] == "SUCCESS",
            "agy did not complete successfully"
        );
        let structured_output = result
            .get("structured_output")
            .filter(|value| value.is_object())
            .context("agy result has no structured object")?
            .clone();
        let conversation_id = result["conversation_id"]
            .as_str()
            .context("agy result has no conversation ID")?
            .parse()?;
        self.completed = Some(Completed {
            structured_output,
            conversation_id,
        });
        Ok(())
    }
}

/// Snapshot SQLite through its backup API, including committed pages still in its WAL.
fn snapshot(source: &Path, destination: &Path) -> Result<()> {
    let source = Connection::open_with_flags(source, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let temporary = destination.with_extension("db.tmp");
    if temporary.exists() {
        fs::remove_file(&temporary)?;
    }
    let mut target = Connection::open(&temporary)?;
    Backup::new(&source, &mut target)?.run_to_completion(256, Duration::from_millis(1), None)?;
    target.close().map_err(|(_, error)| error)?;
    fs::rename(temporary, destination)?;
    Ok(())
}

/// Prepare isolated CLI settings and restore exactly the selected role conversation.
fn prepare(input: &Invocation) -> Result<PathBuf> {
    validate_model(&input.model, &input.effort)?;
    let home = input.scratch.join("agy-home");
    let config = home.join(".gemini/antigravity-cli");
    fs::create_dir_all(config.join("conversations"))?;
    fs::write(
        config.join("settings.json"),
        serde_json::to_vec(
            &json!({"modelProvider":"gemini", "allowNonWorkspaceAccess":true, "artifactReviewPolicy":"always-proceed"}),
        )?,
    )?;
    let mcp = home.join(".gemini/config");
    fs::create_dir_all(&mcp)?;
    fs::write(
        mcp.join("mcp_config.json"),
        serde_json::to_vec(&json!({"mcpServers":input.mcp_servers}))?,
    )?;
    if let Some(id) = input.resume_id {
        let retained = input
            .retained_root
            .as_ref()
            .context("agy continuation has no retained root")?;
        snapshot(
            &retained.join("agy").join(format!("{id}.db")),
            &config.join("conversations").join(format!("{id}.db")),
        )?;
    }
    Ok(home)
}

/// Invoke agy with provider credentials excluded and return its one completed schema result.
pub async fn invoke(input: Invocation, program: &Path, key: &str) -> Result<Completed> {
    let home = prepare(&input)?;
    let project = input.scratch.join("agy-project");
    fs::create_dir_all(project.join(".git"))?;
    let mut command = Command::new(program);
    for (name, _) in env::vars_os() {
        let text = name.to_string_lossy();
        if ["GOOGLE_", "GEMINI_", "AGY_", "CLOUD_CODE_"]
            .iter()
            .any(|prefix| text.starts_with(prefix))
        {
            command.env_remove(name);
        }
    }
    if let Some(agent_home) = env::var_os("HOME") {
        for (name, relative) in [("CARGO_HOME", ".cargo"), ("RUSTUP_HOME", ".rustup")] {
            if env::var_os(name).is_none() {
                command.env(name, Path::new(&agent_home).join(relative));
            }
        }
    }
    command
        .current_dir(&project)
        .args([
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
            "--dangerously-skip-permissions",
            "--model",
            &input.model,
            "--effort",
            &input.effort,
            "--json-schema",
        ])
        .arg(&input.schema_path)
        .env("HOME", &home)
        .env("GEMINI_API_KEY", key)
        .env("GOOGLE_GEMINI_BASE_URL", &input.gateway_url)
        .env("AGY_CLI_DISABLE_AUTO_UPDATE", "true")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(seconds) = input.timeout {
        command.arg("--print-timeout").arg(format!("{seconds}s"));
    }
    if let Some(id) = input.resume_id {
        command.arg("--conversation").arg(id.to_string());
    }
    let mut child = command.spawn().context("could not start agy")?;
    let mut stderr = child.stderr.take().context("agy stderr missing")?;
    let drain = tokio::spawn(async move { io::copy(&mut stderr, &mut io::sink()).await });
    let operation = async {
        let mut stdin = child.stdin.take().context("agy stdin missing")?;
        let prompt = format!(
            "The checkout for this task is {}. Read and edit files there. Begin every shell command with a correctly quoted `cd` to that checkout. All task paths are relative to the checkout.\n\n{}",
            input.workspace.display(),
            input.prompt
        );
        let mut event = serde_json::to_vec(&json!({"event":"user","message":{"content":prompt}}))?;
        event.push(b'\n');
        stdin.write_all(&event).await?;
        drop(stdin);
        let mut lines = BufReader::new(child.stdout.take().context("agy stdout missing")?).lines();
        let mut result = ResultReader::default();
        while let Some(line) = lines.next_line().await? {
            if !line.trim().is_empty() {
                result.event(serde_json::from_str(&line).context("invalid agy event")?)?;
            }
        }
        let status = child.wait().await?;
        if status.code().is_none() {
            return Err(Interrupted::Signal.into());
        }
        ensure!(status.success(), "agy process failed");
        result
            .completed
            .context("agy did not produce a terminal result")
    };
    let result = match input.timeout {
        Some(seconds) => match time::timeout(Duration::from_secs(seconds), operation).await {
            Ok(result) => result,
            Err(_) => Err(Interrupted::Timeout.into()),
        },
        None => operation.await,
    };
    if result.is_err() {
        let _ = child.kill().await;
    }
    drain.abort();
    let completed = result?;
    ensure!(
        input
            .resume_id
            .is_none_or(|id| id == completed.conversation_id),
        "agy resumed another conversation"
    );
    if let Some(root) = input.retained_root {
        let retained = root.join("agy");
        fs::create_dir_all(&retained)?;
        let name = format!("{}.db", completed.conversation_id);
        snapshot(
            &home
                .join(".gemini/antigravity-cli/conversations")
                .join(&name),
            &retained.join(name),
        )?;
    }
    Ok(completed)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Missing, failed, duplicate and nonobject results never reach stage validators.
    #[test]
    fn structured_results_are_terminal_and_unique() {
        let id = "00000000-0000-4000-8000-000000000001";
        let event = json!({"event":"result","result":{"status":"SUCCESS","conversation_id":id,"structured_output":{"ok":true}}});
        let mut reader = ResultReader::default();
        reader
            .event(json!({"event":"message","text":"not a result"}))
            .unwrap();
        assert!(reader.completed.is_none());
        reader.event(event.clone()).unwrap();
        assert!(reader.event(event.clone()).is_err());
        for field in ["status", "conversation_id", "structured_output"] {
            let mut invalid = event.clone();
            invalid["result"][field] = Value::Null;
            assert!(ResultReader::default().event(invalid).is_err());
        }
    }

    /// A retained snapshot includes a committed tool result that has not left SQLite's WAL.
    #[test]
    fn snapshots_include_committed_wal_pages() {
        let directory = std::env::temp_dir().join(format!("agy-wal-{}", Uuid::now_v7()));
        fs::create_dir(&directory).unwrap();
        let source = Connection::open(directory.join("source.db")).unwrap();
        source.execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE turns (result TEXT); INSERT INTO turns VALUES ('tool-result');").unwrap();
        snapshot(&directory.join("source.db"), &directory.join("retained.db")).unwrap();
        let restored = Connection::open(directory.join("retained.db")).unwrap();
        let result: String = restored
            .query_row("SELECT result FROM turns", [], |row| row.get(0))
            .unwrap();
        assert_eq!(result, "tool-result");
        drop((restored, source));
        fs::remove_dir_all(directory).unwrap();
    }
}
