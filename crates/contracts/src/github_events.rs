//! Typed GitHub deliveries other than issues.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::{Actor, Contract, EventKind, IssueState, Label, Repository, Timestamp};

/// Fields shared by every mapped GitHub delivery.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct GithubEvent<P> {
    /// GitHub delivery GUID.
    pub delivery_id: String,
    /// Mapping code revision.
    pub mapping_revision: u32,
    /// Database receipt time.
    pub received_at: Timestamp,
    /// App installation ID.
    pub installation_id: i64,
    /// Repository identity.
    pub repository: Repository,
    /// Actor responsible for the delivery.
    pub sender: Actor,
    /// Open GitHub action name, absent for push and status.
    pub action: Option<String>,
    /// Event-family fields.
    #[serde(flatten)]
    pub payload: P,
}

/// Bind an event-family payload to its subject family.
pub trait GithubPayload {
    /// The family prefix of the NATS subject.
    const FAMILY: &'static str;
}

impl<P: GithubPayload> Contract for GithubEvent<P> {
    const FAMILY: &'static str = P::FAMILY;
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Fact;

    fn event_type(&self) -> String {
        match &self.action {
            Some(action) => format!("{}.{}", P::FAMILY, action),
            None => P::FAMILY.to_owned(),
        }
    }

    fn subject(&self) -> String {
        self.event_type()
    }
}

/// Issue fields consumed from a comment delivery.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct CommentIssue {
    /// Issue number.
    pub number: i64,
    /// Issue title.
    pub title: String,
    /// Current issue state.
    pub state: IssueState,
    /// Whether the issue is a pull request.
    pub is_pull_request: bool,
    /// Issue author.
    pub user: Actor,
    /// Current labels.
    pub labels: Vec<Label>,
    /// Creation time.
    pub created_at: Timestamp,
    /// Latest update time.
    pub updated_at: Timestamp,
}

/// Comment fields used by routing, observations and list projections.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct GithubComment {
    /// Comment ID.
    pub id: i64,
    /// Current body.
    pub body: Option<String>,
    /// Comment author.
    pub user: Actor,
    /// GitHub author association.
    pub author_association: String,
    /// Creation time.
    pub created_at: Timestamp,
    /// Latest update time.
    pub updated_at: Timestamp,
    /// Public comment URL.
    pub html_url: String,
}

/// Issue comment delivery fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct IssueCommentPayload {
    /// Issue snapshot.
    pub issue: CommentIssue,
    /// Comment snapshot.
    pub comment: GithubComment,
    /// Body before an edit, when supplied.
    pub previous_body: Option<String>,
}
impl GithubPayload for IssueCommentPayload {
    const FAMILY: &'static str = "github.issue_comment";
}

/// Pull request head identity.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct PullHead {
    /// Branch name.
    #[serde(rename = "ref")]
    pub branch: String,
    /// Head commit.
    pub sha: String,
    /// Head repository ID, absent after a fork is deleted.
    pub repository_id: Option<i64>,
    /// Head repository full name, absent after a fork is deleted.
    pub full_name: Option<String>,
}

/// Pull request base identity.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct PullBase {
    /// Branch name.
    #[serde(rename = "ref")]
    pub branch: String,
    /// Base commit.
    pub sha: String,
}

/// Pull request snapshot projected by the service.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct GithubPullRequest {
    /// Repository-local PR number.
    pub number: i64,
    /// GraphQL node ID.
    pub node_id: String,
    /// Current title.
    pub title: String,
    /// Current body.
    pub body: Option<String>,
    /// Current state.
    pub state: IssueState,
    /// Draft status.
    pub draft: bool,
    /// Merge status.
    pub merged: bool,
    /// Merge commit if known.
    pub merge_commit_sha: Option<String>,
    /// PR author.
    pub user: Actor,
    /// Head identity.
    pub head: PullHead,
    /// Base identity.
    pub base: PullBase,
    /// Added line count.
    pub additions: i64,
    /// Deleted line count.
    pub deletions: i64,
    /// Latest update time.
    pub updated_at: Timestamp,
}

/// Team requested for review.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct RequestedTeam {
    /// Team ID.
    pub id: i64,
    /// Team slug.
    pub slug: String,
}

/// Pull request delivery fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct PullRequestPayload {
    /// PR snapshot.
    pub pull_request: GithubPullRequest,
    /// Requested individual, if any.
    pub requested_reviewer: Option<Actor>,
    /// Requested team, if any.
    pub requested_team: Option<RequestedTeam>,
}
impl GithubPayload for PullRequestPayload {
    const FAMILY: &'static str = "github.pull_request";
}

/// Review snapshot.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct GithubReview {
    /// Review ID.
    pub id: i64,
    /// Current review state.
    pub state: String,
    /// Review body.
    pub body: Option<String>,
    /// Reviewer.
    pub user: Actor,
    /// Reviewed commit.
    pub commit_id: String,
    /// Submission time, if submitted.
    pub submitted_at: Option<Timestamp>,
}

/// PR identity on a review delivery.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct ReviewPullRequest {
    /// PR number.
    pub number: i64,
    /// Head commit at delivery time.
    pub head_sha: String,
}

/// Pull request review delivery fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct PullRequestReviewPayload {
    /// Review snapshot.
    pub review: GithubReview,
    /// PR identity.
    pub pull_request: ReviewPullRequest,
}
impl GithubPayload for PullRequestReviewPayload {
    const FAMILY: &'static str = "github.pull_request_review";
}

/// Review comment snapshot.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct GithubReviewComment {
    /// Comment ID.
    pub id: i64,
    /// Current body.
    pub body: Option<String>,
    /// Comment author.
    pub user: Actor,
    /// GitHub author association.
    pub author_association: String,
    /// Changed path.
    pub path: String,
    /// Diff line, if available.
    pub line: Option<i64>,
    /// Parent comment, if a reply.
    pub in_reply_to_id: Option<i64>,
    /// Review ID, if attached.
    pub review_id: Option<i64>,
    /// Commit ID.
    pub commit_id: String,
    /// Creation time.
    pub created_at: Timestamp,
    /// Latest update time.
    pub updated_at: Timestamp,
}

/// PR identity on a review comment or thread.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct NumberedPullRequest {
    /// PR number.
    pub number: i64,
}

/// Pull request review comment delivery fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct PullRequestReviewCommentPayload {
    /// Review comment snapshot.
    pub comment: GithubReviewComment,
    /// PR identity.
    pub pull_request: NumberedPullRequest,
    /// Body before an edit, when supplied.
    pub previous_body: Option<String>,
}
impl GithubPayload for PullRequestReviewCommentPayload {
    const FAMILY: &'static str = "github.pull_request_review_comment";
}

/// Review thread identity.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct GithubReviewThread {
    /// GraphQL node ID.
    pub node_id: String,
    /// First comment ID in the thread.
    pub first_comment_id: i64,
}

/// Pull request review thread delivery fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct PullRequestReviewThreadPayload {
    /// Thread identity.
    pub thread: GithubReviewThread,
    /// PR identity.
    pub pull_request: NumberedPullRequest,
}
impl GithubPayload for PullRequestReviewThreadPayload {
    const FAMILY: &'static str = "github.pull_request_review_thread";
}

/// Push delivery fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct PushPayload {
    /// Updated ref.
    #[serde(rename = "ref")]
    pub reference: String,
    /// Previous commit.
    pub before: String,
    /// New commit.
    pub after: String,
    /// Whether the ref moved non-fast-forward.
    pub forced: bool,
}
impl GithubPayload for PushPayload {
    const FAMILY: &'static str = "github.push";
}

/// Commit status delivery fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct StatusPayload {
    /// Status ID.
    pub id: i64,
    /// Commit SHA.
    pub sha: String,
    /// Status context.
    pub context: String,
    /// Status state.
    pub state: String,
}
impl GithubPayload for StatusPayload {
    const FAMILY: &'static str = "github.status";
}

/// Check run delivery fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct CheckRunPayload {
    /// Check run ID.
    pub id: i64,
    /// Check name.
    pub name: String,
    /// Current status.
    pub status: String,
    /// Final conclusion, if any.
    pub conclusion: Option<String>,
    /// Head commit.
    pub head_sha: String,
    /// Owning GitHub App ID, if any.
    pub app_id: Option<i64>,
    /// Check suite ID.
    pub check_suite_id: Option<i64>,
    /// External ID, if supplied.
    pub external_id: Option<String>,
    /// Related PR numbers.
    pub pull_requests: Vec<i64>,
}
impl GithubPayload for CheckRunPayload {
    const FAMILY: &'static str = "github.check_run";
}

/// Check suite delivery fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct CheckSuitePayload {
    /// Check suite ID.
    pub id: i64,
    /// Current status.
    pub status: String,
    /// Final conclusion, if any.
    pub conclusion: Option<String>,
    /// Head commit.
    pub head_sha: String,
    /// Owning App slug, if any.
    pub app_slug: Option<String>,
    /// Number of latest check runs.
    pub latest_check_runs_count: Option<i64>,
    /// Related PR numbers.
    pub pull_requests: Vec<i64>,
}
impl GithubPayload for CheckSuitePayload {
    const FAMILY: &'static str = "github.check_suite";
}

/// Workflow run delivery fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct WorkflowRunPayload {
    /// Workflow run ID.
    pub id: i64,
    /// Attempt number.
    pub run_attempt: i64,
    /// Workflow ID.
    pub workflow_id: i64,
    /// Display name.
    pub name: String,
    /// Workflow file path.
    pub path: String,
    /// GitHub trigger event.
    pub event: String,
    /// Current status.
    pub status: String,
    /// Final conclusion, if any.
    pub conclusion: Option<String>,
    /// Head branch, if any.
    pub head_branch: Option<String>,
    /// Head commit.
    pub head_sha: String,
    /// Check suite ID.
    pub check_suite_id: Option<i64>,
    /// Creation time.
    pub created_at: Timestamp,
    /// Run start time, if started.
    pub run_started_at: Option<Timestamp>,
    /// Latest update time.
    pub updated_at: Timestamp,
    /// Related PR numbers.
    pub pull_requests: Vec<i64>,
}
impl GithubPayload for WorkflowRunPayload {
    const FAMILY: &'static str = "github.workflow_run";
}
