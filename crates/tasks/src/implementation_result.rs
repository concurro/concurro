//! Source-aware validation of completed implementer workspace changes.

use anyhow::{Context, Result};
use concurro_contracts::TaskId;
use concurro_db::Database;
use concurro_tasks_core::{
    ImplementationEdit, ImplementationReport, InitializationSnapshot, validate_design_issue,
    validate_edits, validated_edit_paths,
};
use concurro_tasks_postgres::{
    ImplementationAssessment, ImplementationInputs, ImplementationResult,
    apply_implementation_result, finish_unapplied_result, implementation_inputs,
    implementation_result, initialization_snapshot,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{InitializationTarget, IntakeContext, initialization};

/// A Graphile Worker job for one retained native implementation result.
#[derive(Deserialize, Serialize)]
pub struct TaskImplementationResultJob {
    /// Exchange turn supplying the immutable response and workspace extraction.
    pub turn: Uuid,
}

impl TaskHandler for TaskImplementationResultJob {
    const IDENTIFIER: &'static str = "task_implementation_result";

    /// Read pinned source, then atomically accept or reject one candidate.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let intake = context
            .get_ext::<IntakeContext>()
            .ok_or("implementation result context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_implementation_result(&database, intake, self.turn)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Apply a completed native result only after checking the approved parent files.
pub async fn run_task_implementation_result(
    database: &Database,
    context: &IntakeContext,
    turn: Uuid,
) -> Result<()> {
    let Some(result) = implementation_result(database, turn).await? else {
        let mut commit = database.begin().await?;
        finish_unapplied_result(&mut commit, &context.initialization.outbox, turn).await?;
        commit.commit().await?;
        return Ok(());
    };
    let snapshot = initialization_snapshot(database, TaskId(result.task_id))
        .await?
        .context("implementation source snapshot unavailable")?;
    let Some(inputs) = implementation_inputs(database, result.task_id).await? else {
        let mut commit = database.begin().await?;
        finish_unapplied_result(&mut commit, &context.initialization.outbox, turn).await?;
        commit.commit().await?;
        return Ok(());
    };
    let target = context
        .initialization
        .targets
        .get(&result.issue.repository_id)
        .context("implementation target unavailable")?;
    let candidate = validate_candidate(context, target, &result, &snapshot, &inputs).await?;
    let mut commit = database.begin().await?;
    let applied =
        apply_implementation_result(&mut commit, &context.initialization.outbox, turn, candidate)
            .await?;
    if !applied {
        finish_unapplied_result(&mut commit, &context.initialization.outbox, turn).await?;
    }
    commit.commit().await?;
    Ok(())
}

/// Return a frozen candidate or a permanent validation reason without a Git write.
async fn validate_candidate(
    context: &IntakeContext,
    target: &InitializationTarget,
    result: &ImplementationResult,
    snapshot: &InitializationSnapshot,
    inputs: &ImplementationInputs,
) -> Result<ImplementationAssessment> {
    let response = &result.result["response"];
    let workspace = &result.result["workspace"];
    if response.as_object().is_none_or(|fields| {
        fields.len() != 5
            || ![
                "summary",
                "validation",
                "design_issue",
                "description",
                "commit",
            ]
            .iter()
            .all(|key| fields.contains_key(*key))
    }) || response["summary"]
        .as_str()
        .is_none_or(|summary| summary.trim().is_empty())
        || !response["validation"].is_string()
        || !response["description"].is_object()
        || !response["commit"].is_object()
    {
        return Ok(ImplementationAssessment::Invalid(
            "invalid implementation response".to_owned(),
        ));
    }
    if !response["design_issue"].is_null() {
        if !workspace.is_object()
            || !workspace["error"].is_null()
            || workspace["edits"]
                .as_array()
                .is_none_or(|edits| !edits.is_empty())
            || workspace["removed"]
                .as_array()
                .is_none_or(|removed| !removed.is_empty())
        {
            return Ok(ImplementationAssessment::Invalid(
                "design reopening cannot include candidate edits".to_owned(),
            ));
        }
        return Ok(
            match validate_design_issue(response["design_issue"].clone()) {
                Ok(issue) => ImplementationAssessment::Reopen(issue),
                Err(error) => ImplementationAssessment::Invalid(error),
            },
        );
    }
    let sentence = |value: &Value| {
        value
            .as_str()
            .is_some_and(|text| !text.trim().is_empty() && !text.contains(['\r', '\n', '\0']))
    };
    let bullets = |value: &Value, minimum: usize| {
        value
            .as_array()
            .is_some_and(|lines| lines.len() >= minimum && lines.iter().all(sentence))
    };
    let description = &response["description"];
    let commit = &response["commit"];
    let valid_description = description.as_object().is_some_and(|fields| {
        fields.len() == 5
            && [
                "lead",
                "problem",
                "changes",
                "compatibility",
                "review_focus",
            ]
            .iter()
            .all(|key| fields.contains_key(*key))
    }) && sentence(&description["lead"])
        && bullets(&description["problem"], 1)
        && bullets(&description["changes"], 1)
        && bullets(&description["compatibility"], 0)
        && bullets(&description["review_focus"], 0);
    let valid_commit = commit.as_object().is_some_and(|fields| {
        fields.len() == 3
            && ["subject", "type", "changes"]
                .iter()
                .all(|key| fields.contains_key(*key))
    }) && sentence(&commit["subject"])
        && matches!(
            commit["type"].as_str(),
            Some(
                "feat"
                    | "fix"
                    | "docs"
                    | "refactor"
                    | "perf"
                    | "test"
                    | "build"
                    | "ci"
                    | "chore"
                    | "revert"
            )
        )
        && bullets(&commit["changes"], 1);
    if !valid_description || !valid_commit {
        return Ok(ImplementationAssessment::Invalid(
            "invalid implementation description or commit metadata".to_owned(),
        ));
    }
    if !workspace.is_object() {
        return Ok(ImplementationAssessment::Invalid(
            "implementation turn returned no workspace changes".to_owned(),
        ));
    }
    if let Some(error) = workspace["error"].as_str() {
        return Ok(ImplementationAssessment::Invalid(format!(
            "workspace extraction failed: {error}"
        )));
    }
    let Some(native_edits) = workspace["edits"].as_array() else {
        return Ok(ImplementationAssessment::Invalid(
            "workspace edits are missing".to_owned(),
        ));
    };
    let Some(removed) = workspace["removed"].as_array() else {
        return Ok(ImplementationAssessment::Invalid(
            "workspace removals are missing".to_owned(),
        ));
    };
    let mut edits = native_edits.clone();
    for path in removed {
        let Some(path) = path.as_str() else {
            return Ok(ImplementationAssessment::Invalid(
                "workspace removal path is invalid".to_owned(),
            ));
        };
        edits.push(json!({"path":path,"content":null}));
    }
    if edits.is_empty() && inputs.branch.is_some() {
        if inputs.current_description == *description || inputs.current_validation.is_null() {
            return Ok(ImplementationAssessment::Invalid(
                "description-only repair has no changed description or prior validation".to_owned(),
            ));
        }
        return Ok(ImplementationAssessment::Candidate(json!({
            "turn":result.turn,
            "plan_id":inputs.plan_id,
            "binding":inputs.authorization["binding"],
            "parent":inputs.parent,
            "summary":response["summary"],
            "edits":[],
            "originals":{},
            "validation":inputs.current_validation,
            "description":description,
            "commit":commit,
        })));
    }
    let report: ImplementationReport = match serde_json::from_value(json!({
        "summary":response["summary"],"edits":edits
    })) {
        Ok(report) => report,
        Err(_) => {
            return Ok(ImplementationAssessment::Invalid(
                "invalid implementation edits".to_owned(),
            ));
        }
    };
    let paths = match validated_edit_paths(&report, &inputs.plan, &snapshot.configuration) {
        Ok(paths) => paths,
        Err(error) => return Ok(ImplementationAssessment::Invalid(error)),
    };
    let (source_repository, source_permissions) = if inputs.branch.is_some() {
        (&target.fork, &target.fork_permissions)
    } else {
        (&target.upstream, &target.upstream_permissions)
    };
    let originals = match initialization::read_source_files(
        &context.initialization.github,
        source_repository,
        source_permissions,
        &inputs.parent,
        &paths,
    )
    .await?
    {
        Ok(originals) => originals,
        Err(reason) => return Ok(ImplementationAssessment::Invalid(reason)),
    };
    let validated = match validate_edits(
        serde_json::to_value(&report)?,
        &inputs.plan,
        &originals,
        &snapshot.configuration,
    ) {
        Ok(validated) => validated,
        Err(error) => return Ok(ImplementationAssessment::Invalid(error)),
    };
    let edits: Vec<ImplementationEdit> = validated.edits;
    Ok(ImplementationAssessment::Candidate(json!({
        "turn":result.turn,
        "plan_id":inputs.plan_id,
        "binding":inputs.authorization["binding"],
        "parent":inputs.parent,
        "summary":validated.summary,
        "edits":edits,
        "originals":originals,
        "validation":response["validation"],
        "description":response["description"],
        "commit":response["commit"],
    })))
}
