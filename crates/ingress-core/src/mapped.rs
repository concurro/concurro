//! Pure mapping of GitHub webhook families beyond issues.

use chrono::{DateTime, Utc};
use concurro_contracts::{
    Actor, CheckRunPayload, CheckSuitePayload, CommentIssue, Envelope, EventId, GithubComment,
    GithubEvent, GithubPayload, GithubPullRequest, GithubReview, GithubReviewComment,
    GithubReviewThread, IssueCommentPayload, IssueState, Label, NumberedPullRequest, PullBase,
    PullHead, PullRequestPayload, PullRequestReviewCommentPayload, PullRequestReviewPayload,
    PullRequestReviewThreadPayload, PushPayload, RequestedTeam, ReviewPullRequest, StatusPayload,
    Timestamp, WorkflowRunPayload,
};
use serde::{Deserialize, de::DeserializeOwned};
use serde_json::Value;

use crate::{MAPPING_REVISION, RawCommon, RawId, RawRepository};

/// One of the typed GitHub events a signed delivery may publish.
#[derive(Clone, Debug, PartialEq)]
pub enum MappedEvent {
    /// Issues event.
    Issues(Envelope<concurro_contracts::GithubIssues>),
    /// Issue comment event.
    IssueComment(Envelope<GithubEvent<IssueCommentPayload>>),
    /// Pull request event.
    PullRequest(Envelope<GithubEvent<PullRequestPayload>>),
    /// Review event.
    PullRequestReview(Envelope<GithubEvent<PullRequestReviewPayload>>),
    /// Review comment event.
    PullRequestReviewComment(Envelope<GithubEvent<PullRequestReviewCommentPayload>>),
    /// Review thread event.
    PullRequestReviewThread(Envelope<GithubEvent<PullRequestReviewThreadPayload>>),
    /// Push event.
    Push(Envelope<GithubEvent<PushPayload>>),
    /// Commit status event.
    Status(Envelope<GithubEvent<StatusPayload>>),
    /// Check run event.
    CheckRun(Envelope<GithubEvent<CheckRunPayload>>),
    /// Check suite event.
    CheckSuite(Envelope<GithubEvent<CheckSuitePayload>>),
    /// Workflow run event.
    WorkflowRun(Envelope<GithubEvent<WorkflowRunPayload>>),
}

/// Decode only fields a mapper needs, ignoring other GitHub payload fields.
fn input<T: DeserializeOwned>(value: &Value) -> Result<T, String> {
    serde_json::from_value(value.clone()).map_err(|error| error.to_string())
}

/// Build a checked fact with the shared GitHub delivery identity.
fn event<P: GithubPayload + serde::Serialize>(
    delivery_id: &str,
    received_at: DateTime<Utc>,
    common: RawCommon,
    payload: P,
) -> Result<Envelope<GithubEvent<P>>, String> {
    let mapped = GithubEvent {
        delivery_id: delivery_id.to_owned(),
        mapping_revision: MAPPING_REVISION,
        received_at: Timestamp(received_at),
        installation_id: common.installation.id,
        repository: common.repository.into(),
        sender: common.sender,
        action: common.action,
        payload,
    };
    let envelope = Envelope::fact(
        EventId::github(delivery_id, MAPPING_REVISION),
        "ingress",
        Timestamp(received_at),
        mapped,
    );
    envelope.validate().map_err(|error| error.to_string())?;
    Ok(envelope)
}

/// Map one modeled family; return None for a family with no contract.
pub fn map_other(
    delivery_id: &str,
    family: &str,
    value: &Value,
    received_at: DateTime<Utc>,
) -> Option<Result<MappedEvent, String>> {
    let action_required = match family {
        "push" | "status" => false,
        "issue_comment"
        | "pull_request"
        | "pull_request_review"
        | "pull_request_review_comment"
        | "pull_request_review_thread"
        | "check_run"
        | "check_suite"
        | "workflow_run" => true,
        _ => return None,
    };
    let common: RawCommon = match input(value) {
        Ok(common) => common,
        Err(error) => return Some(Err(error)),
    };
    if common.action.is_some() != action_required {
        return Some(Err(format!("invalid action for {family}")));
    }
    let mapped = match family {
        "issue_comment" => map_issue_comment(value).and_then(|payload| {
            event(delivery_id, received_at, common, payload).map(MappedEvent::IssueComment)
        }),
        "pull_request" => map_pull_request(value).and_then(|payload| {
            event(delivery_id, received_at, common, payload).map(MappedEvent::PullRequest)
        }),
        "pull_request_review" => map_review(value).and_then(|payload| {
            event(delivery_id, received_at, common, payload).map(MappedEvent::PullRequestReview)
        }),
        "pull_request_review_comment" => map_review_comment(value).and_then(|payload| {
            event(delivery_id, received_at, common, payload)
                .map(MappedEvent::PullRequestReviewComment)
        }),
        "pull_request_review_thread" => map_review_thread(value).and_then(|payload| {
            event(delivery_id, received_at, common, payload)
                .map(MappedEvent::PullRequestReviewThread)
        }),
        "push" => input(value).and_then(|payload: PushPayload| {
            event(delivery_id, received_at, common, payload).map(MappedEvent::Push)
        }),
        "status" => input(value).and_then(|payload: StatusPayload| {
            event(delivery_id, received_at, common, payload).map(MappedEvent::Status)
        }),
        "check_run" => map_check_run(value).and_then(|payload| {
            event(delivery_id, received_at, common, payload).map(MappedEvent::CheckRun)
        }),
        "check_suite" => map_check_suite(value).and_then(|payload| {
            event(delivery_id, received_at, common, payload).map(MappedEvent::CheckSuite)
        }),
        "workflow_run" => map_workflow_run(value).and_then(|payload| {
            event(delivery_id, received_at, common, payload).map(MappedEvent::WorkflowRun)
        }),
        _ => return None,
    };
    Some(mapped)
}

/// GitHub issue fields carried by an issue comment.
#[derive(Deserialize)]
struct RawCommentIssue {
    number: i64,
    title: String,
    state: IssueState,
    user: Actor,
    labels: Vec<Label>,
    created_at: Timestamp,
    updated_at: Timestamp,
    pull_request: Option<Value>,
}

/// Body change object from a GitHub edited action.
#[derive(Deserialize)]
struct RawBodyChange {
    #[serde(rename = "from")]
    previous: Option<String>,
}

/// GitHub's optional change fields.
#[derive(Deserialize)]
struct RawChanges {
    body: Option<RawBodyChange>,
}

/// GitHub issue comment input.
#[derive(Deserialize)]
struct RawIssueComment {
    issue: RawCommentIssue,
    comment: GithubComment,
    changes: Option<RawChanges>,
}

/// Map issue comment fields and previous body.
fn map_issue_comment(value: &Value) -> Result<IssueCommentPayload, String> {
    let raw: RawIssueComment = input(value)?;
    Ok(IssueCommentPayload {
        issue: CommentIssue {
            number: raw.issue.number,
            title: raw.issue.title,
            state: raw.issue.state,
            is_pull_request: raw.issue.pull_request.is_some(),
            user: raw.issue.user,
            labels: raw.issue.labels,
            created_at: raw.issue.created_at,
            updated_at: raw.issue.updated_at,
        },
        comment: raw.comment,
        previous_body: raw
            .changes
            .and_then(|changes| changes.body)
            .and_then(|body| body.previous),
    })
}

/// GitHub head branch shape.
#[derive(Deserialize)]
struct RawHead {
    #[serde(rename = "ref")]
    branch: String,
    sha: String,
    repo: Option<RawRepository>,
}

/// PR fields read from a webhook payload.
#[derive(Deserialize)]
struct RawPull {
    number: i64,
    node_id: String,
    title: String,
    body: Option<String>,
    state: IssueState,
    draft: bool,
    merged: bool,
    merge_commit_sha: Option<String>,
    user: Actor,
    head: RawHead,
    base: PullBase,
    additions: i64,
    deletions: i64,
    updated_at: Timestamp,
}

/// Pull request event input.
#[derive(Deserialize)]
struct RawPullEvent {
    pull_request: RawPull,
    requested_reviewer: Option<Actor>,
    requested_team: Option<RequestedTeam>,
}

/// Map a pull request snapshot and optional review request.
fn map_pull_request(value: &Value) -> Result<PullRequestPayload, String> {
    let raw: RawPullEvent = input(value)?;
    let pull = raw.pull_request;
    Ok(PullRequestPayload {
        pull_request: GithubPullRequest {
            number: pull.number,
            node_id: pull.node_id,
            title: pull.title,
            body: pull.body,
            state: pull.state,
            draft: pull.draft,
            merged: pull.merged,
            merge_commit_sha: pull.merge_commit_sha,
            user: pull.user,
            head: PullHead {
                branch: pull.head.branch,
                sha: pull.head.sha,
                repository_id: pull.head.repo.as_ref().map(|repo| repo.id),
                full_name: pull.head.repo.map(|repo| repo.full_name),
            },
            base: pull.base,
            additions: pull.additions,
            deletions: pull.deletions,
            updated_at: pull.updated_at,
        },
        requested_reviewer: raw.requested_reviewer,
        requested_team: raw.requested_team,
    })
}

/// Review event input.
#[derive(Deserialize)]
struct RawReviewEvent {
    review: GithubReview,
    pull_request: RawReviewPull,
}

/// GitHub review PR with nested head.
#[derive(Deserialize)]
struct RawReviewPull {
    number: i64,
    head: RawIdSha,
}

/// Object with a SHA.
#[derive(Deserialize)]
struct RawIdSha {
    sha: String,
}

/// Map a pull request review.
fn map_review(value: &Value) -> Result<PullRequestReviewPayload, String> {
    let raw: RawReviewEvent = input(value)?;
    Ok(PullRequestReviewPayload {
        review: raw.review,
        pull_request: ReviewPullRequest {
            number: raw.pull_request.number,
            head_sha: raw.pull_request.head.sha,
        },
    })
}

/// Review comment input.
#[derive(Deserialize)]
struct RawReviewCommentEvent {
    comment: RawReviewComment,
    pull_request: NumberedPullRequest,
    changes: Option<RawChanges>,
}

/// GitHub's review-comment field name before contract normalization.
#[derive(Deserialize)]
struct RawReviewComment {
    #[serde(flatten)]
    comment: GithubReviewComment,
    pull_request_review_id: Option<i64>,
}

/// Map a review comment and previous body.
fn map_review_comment(value: &Value) -> Result<PullRequestReviewCommentPayload, String> {
    let raw: RawReviewCommentEvent = input(value)?;
    let mut comment = raw.comment.comment;
    comment.review_id = raw.comment.pull_request_review_id;
    Ok(PullRequestReviewCommentPayload {
        comment,
        pull_request: raw.pull_request,
        previous_body: raw
            .changes
            .and_then(|changes| changes.body)
            .and_then(|body| body.previous),
    })
}

/// Review thread input.
#[derive(Deserialize)]
struct RawReviewThreadEvent {
    thread: RawReviewThread,
    pull_request: NumberedPullRequest,
}

/// GitHub review thread with its ordered comments.
#[derive(Deserialize)]
struct RawReviewThread {
    /// GraphQL thread identity.
    node_id: String,
    /// Comments beginning with the thread root.
    comments: Vec<RawId>,
}

/// Map a review thread identity.
fn map_review_thread(value: &Value) -> Result<PullRequestReviewThreadPayload, String> {
    let raw: RawReviewThreadEvent = input(value)?;
    let first_comment_id = raw
        .thread
        .comments
        .first()
        .ok_or("review thread has no comments")?
        .id;
    Ok(PullRequestReviewThreadPayload {
        thread: GithubReviewThread {
            node_id: raw.thread.node_id,
            first_comment_id,
        },
        pull_request: raw.pull_request,
    })
}

/// Related PR number in a check or run payload.
#[derive(Deserialize)]
struct RawPullNumber {
    number: i64,
}

/// Check run input.
#[derive(Deserialize)]
struct RawCheckRun {
    id: i64,
    name: String,
    status: String,
    conclusion: Option<String>,
    head_sha: String,
    app: Option<RawId>,
    check_suite: Option<RawId>,
    external_id: Option<String>,
    #[serde(default)]
    pull_requests: Vec<RawPullNumber>,
}

/// Check run wrapper.
#[derive(Deserialize)]
struct RawCheckRunEvent {
    check_run: RawCheckRun,
}

/// Map a check run and related PR numbers.
fn map_check_run(value: &Value) -> Result<CheckRunPayload, String> {
    let raw: RawCheckRunEvent = input(value)?;
    let check = raw.check_run;
    Ok(CheckRunPayload {
        id: check.id,
        name: check.name,
        status: check.status,
        conclusion: check.conclusion,
        head_sha: check.head_sha,
        app_id: check.app.map(|app| app.id),
        check_suite_id: check.check_suite.map(|suite| suite.id),
        external_id: check.external_id,
        pull_requests: check
            .pull_requests
            .into_iter()
            .map(|pull| pull.number)
            .collect(),
    })
}

/// GitHub App slug on a check suite.
#[derive(Deserialize)]
struct RawAppSlug {
    slug: String,
}

/// Check suite input.
#[derive(Deserialize)]
struct RawCheckSuite {
    id: i64,
    status: String,
    conclusion: Option<String>,
    head_sha: String,
    app: Option<RawAppSlug>,
    latest_check_runs_count: Option<i64>,
    #[serde(default)]
    pull_requests: Vec<RawPullNumber>,
}

/// Check suite wrapper.
#[derive(Deserialize)]
struct RawCheckSuiteEvent {
    check_suite: RawCheckSuite,
}

/// Map a check suite and related PR numbers.
fn map_check_suite(value: &Value) -> Result<CheckSuitePayload, String> {
    let raw: RawCheckSuiteEvent = input(value)?;
    let check = raw.check_suite;
    Ok(CheckSuitePayload {
        id: check.id,
        status: check.status,
        conclusion: check.conclusion,
        head_sha: check.head_sha,
        app_slug: check.app.map(|app| app.slug),
        latest_check_runs_count: check.latest_check_runs_count,
        pull_requests: check
            .pull_requests
            .into_iter()
            .map(|pull| pull.number)
            .collect(),
    })
}

/// Workflow run input.
#[derive(Deserialize)]
struct RawWorkflowRun {
    id: i64,
    run_attempt: i64,
    workflow_id: i64,
    name: String,
    path: String,
    event: String,
    status: String,
    conclusion: Option<String>,
    head_branch: Option<String>,
    head_sha: String,
    check_suite_id: Option<i64>,
    created_at: Timestamp,
    run_started_at: Option<Timestamp>,
    updated_at: Timestamp,
    #[serde(default)]
    pull_requests: Vec<RawPullNumber>,
}

/// Workflow run wrapper.
#[derive(Deserialize)]
struct RawWorkflowRunEvent {
    workflow_run: RawWorkflowRun,
}

/// Map a workflow run and related PR numbers.
fn map_workflow_run(value: &Value) -> Result<WorkflowRunPayload, String> {
    let raw: RawWorkflowRunEvent = input(value)?;
    let run = raw.workflow_run;
    Ok(WorkflowRunPayload {
        id: run.id,
        run_attempt: run.run_attempt,
        workflow_id: run.workflow_id,
        name: run.name,
        path: run.path,
        event: run.event,
        status: run.status,
        conclusion: run.conclusion,
        head_branch: run.head_branch,
        head_sha: run.head_sha,
        check_suite_id: run.check_suite_id,
        created_at: run.created_at,
        run_started_at: run.run_started_at,
        updated_at: run.updated_at,
        pull_requests: run
            .pull_requests
            .into_iter()
            .map(|pull| pull.number)
            .collect(),
    })
}
