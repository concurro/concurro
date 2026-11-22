//! Contract-specific schema adjustments.

use serde_json::{Map, Value, json};

use crate::GithubPayload;

/// Restrict the target snapshot to its one configuration entity.
pub(crate) fn prepare_config_targets(schema: &mut Value) {
    prepare_task_snapshot(schema);
    schema["properties"]["type"] = json!({"const":"config.targets"});
    schema["properties"]["entity"] = json!({"const":"config:targets"});
}

/// Restrict complete read-model records to their snapshot subject.
pub(crate) fn prepare_read_model_record(schema: &mut Value) {
    prepare_task_snapshot(schema);
    schema["properties"]["type"] = json!({"const":"view.record"});
}

/// Restrict request outcome facts to the implemented dispositions.
pub(crate) fn prepare_request_outcome(schema: &mut Value) {
    schema["properties"]["version"] = json!({"const": 2});
    schema["properties"]["kind"] = json!({"const": "fact"});
    schema["properties"]["type"] =
        json!({"type": "string", "pattern": "^request\\.tasks\\.(?:applied|refused|superseded)$"});
    schema["properties"]["entity"] = json!({"type": "null"});
    schema["properties"]["revision"] = json!({"type": "null"});
}

/// Restrict task state snapshots to their task-scoped subject and metadata.
pub(crate) fn prepare_task_snapshot(schema: &mut Value) {
    schema["properties"]["version"] = json!({"const": 1});
    schema["properties"]["kind"] = json!({"const": "snapshot"});
    schema["properties"]["type"] =
        json!({"type": "string", "pattern": "^task\\.[0-9a-f-]{36}\\.state$"});
    schema["properties"]["entity"] = json!({"type": "string", "minLength": 1});
    schema["properties"]["revision"] = json!({"type": "integer", "minimum": 1});
}

/// Restrict notice snapshots to task-scoped subjects.
pub(crate) fn prepare_task_notice(schema: &mut Value) {
    prepare_task_snapshot(schema);
    schema["properties"]["type"] =
        json!({"type":"string","pattern":"^task\\.[0-9a-f-]{36}\\.notice$"});
}

/// Restrict report snapshots to task-scoped subjects.
pub(crate) fn prepare_task_report(schema: &mut Value) {
    prepare_task_snapshot(schema);
    schema["properties"]["type"] =
        json!({"type":"string","pattern":"^task\\.[0-9a-f-]{36}\\.report$"});
}

/// Restrict submitted facts to one turn identity.
pub(crate) fn prepare_turn_submitted(schema: &mut Value) {
    prepare_turn_fact(schema, "submitted");
}

/// Restrict finished facts to one turn identity.
pub(crate) fn prepare_turn_finished(schema: &mut Value) {
    prepare_turn_fact(schema, "finished");
}

/// Restrict a turn fact's subject and absent snapshot metadata.
fn prepare_turn_fact(schema: &mut Value, suffix: &str) {
    schema["properties"]["version"] = json!({"const":1});
    schema["properties"]["kind"] = json!({"const":"fact"});
    schema["properties"]["type"] =
        json!({"type":"string","pattern":format!("^turn\\.[0-9a-f-]{{36}}\\.{suffix}$")});
    schema["properties"]["entity"] = json!({"type":"null"});
    schema["properties"]["revision"] = json!({"type":"null"});
}

/// Restrict status snapshots to turn-scoped subjects and revisions.
pub(crate) fn prepare_turn_status(schema: &mut Value) {
    prepare_task_snapshot(schema);
    schema["properties"]["type"] =
        json!({"type":"string","pattern":"^turn\\.[0-9a-f-]{36}\\.status$"});
}

/// Restrict exchange lifecycle snapshots to their turn-scoped subject.
pub(crate) fn prepare_exchange_turn_status(schema: &mut Value) {
    prepare_task_snapshot(schema);
    schema["properties"]["type"] =
        json!({"type":"string","pattern":"^exchange\\.turn\\.[0-9a-f-]{36}\\.status$"});
}

/// Restrict terminal exchange facts to their turn-scoped subject.
pub(crate) fn prepare_exchange_turn_finished(schema: &mut Value) {
    prepare_turn_fact(schema, "finished");
    schema["properties"]["type"] =
        json!({"type":"string","pattern":"^exchange\\.turn\\.[0-9a-f-]{36}\\.finished$"});
}

/// Restrict accounting facts to the inference subject.
pub(crate) fn prepare_inference_usage(schema: &mut Value) {
    fix_fact_envelope(schema, "inference.usage");
}

/// Fix the envelope metadata for an action-bearing or bare GitHub fact.
pub(crate) fn prepare_github_event<P: GithubPayload>(schema: &mut Value) {
    schema["properties"]["version"] = json!({"const": 1});
    schema["properties"]["kind"] = json!({"const": "fact"});
    schema["properties"]["type"] = json!({
        "type": "string",
        "pattern": format!("^{}(?:\\.[^.\\s*>\\u0000-\\u001f\\u007f-\\u009f]+)?$", P::FAMILY.replace('.', "\\."))
    });
    schema["properties"]["entity"] = json!({"type": "null"});
    schema["properties"]["revision"] = json!({"type": "null"});
}

/// Fix the coverage-advanced fact's type and metadata.
pub(crate) fn prepare_coverage_advanced(schema: &mut Value) {
    fix_fact_envelope(schema, "ingress.coverage.advanced");
}

/// Fix the coverage-gap fact's type and metadata.
pub(crate) fn prepare_coverage_gap(schema: &mut Value) {
    fix_fact_envelope(schema, "ingress.coverage.gap");
}

/// Pin the wire metadata of one nonvariant fact.
fn fix_fact_envelope(schema: &mut Value, event_type: &str) {
    schema["properties"]["version"] = json!({"const": 1});
    schema["properties"]["kind"] = json!({"const": "fact"});
    schema["properties"]["type"] = json!({"const": event_type});
    schema["properties"]["entity"] = json!({"type": "null"});
    schema["properties"]["revision"] = json!({"type": "null"});
}

/// Finalize the GitHub issues schema for the documented wire shape.
pub(crate) fn prepare_github_issues(schema: &mut Value) {
    add_discriminator(schema);
    flatten_issue_schema(schema);
    fix_issue_envelope(schema);
}

/// Mark the action union so Python generation selects its variant by tag.
fn add_discriminator(schema: &mut Value) {
    if let Some(action) = schema.pointer_mut("/$defs/KnownIssueAction") {
        action["discriminator"] = json!({"propertyName": "action"});
    }
}

/// Expand the flattened wire fields into branches Python can validate exactly.
fn flatten_issue_schema(schema: &mut Value) {
    let common = schema
        .pointer("/$defs/GithubIssues")
        .expect("issues definition")
        .clone();
    let known = schema
        .pointer("/$defs/KnownIssueAction/oneOf")
        .expect("known actions");
    let known_branches: Vec<Value> = known
        .as_array()
        .expect("action branches")
        .iter()
        .map(|branch| merge_issue_branch(&common, branch))
        .collect();
    let fallback = common.pointer("/anyOf/1").expect("open action fallback");
    let fallback = merge_issue_branch(&common, fallback);
    let issue = schema
        .pointer_mut("/$defs/GithubIssues")
        .expect("issues definition");
    let object = issue.as_object_mut().expect("issues object");
    object.remove("properties");
    object.remove("required");
    object.insert(
        "anyOf".to_owned(),
        json!([
            {"oneOf": known_branches, "discriminator": {"propertyName": "action"}},
            fallback
        ]),
    );
    schema["$defs"]
        .as_object_mut()
        .expect("definitions")
        .remove("KnownIssueAction");
}

/// Combine common delivery fields with one action's own fields.
fn merge_issue_branch(common: &Value, branch: &Value) -> Value {
    let mut result = branch.clone();
    let mut properties = common["properties"]
        .as_object()
        .expect("common properties")
        .clone();
    properties.extend(
        branch["properties"]
            .as_object()
            .expect("action properties")
            .clone(),
    );
    let mut required = common["required"]
        .as_array()
        .expect("common required")
        .clone();
    required.extend(
        branch["required"]
            .as_array()
            .expect("action required")
            .clone(),
    );
    let object: &mut Map<String, Value> = result.as_object_mut().expect("branch object");
    object.insert("properties".to_owned(), Value::Object(properties));
    object.insert("required".to_owned(), Value::Array(required));
    object.insert("type".to_owned(), json!("object"));
    result
}

/// Fix this contract's version, kind and NATS-safe family in its schema.
fn fix_issue_envelope(schema: &mut Value) {
    schema["properties"]["version"] = json!({"const": 1, "description": "Contract major version."});
    schema["properties"]["kind"] = json!({"const": "fact", "description": "Fact event."});
    schema["properties"]["type"] = json!({
        "type": "string",
        "pattern": "^github\\.issues\\.[^.\\s*>\\u0000-\\u001f\\u007f-\\u009f]+$(?![\\s\\S])",
        "description": "Concrete GitHub issue action type."
    });
    schema["properties"]["entity"] =
        json!({"type": "null", "description": "Facts have no snapshot entity."});
    schema["properties"]["revision"] =
        json!({"type": "null", "description": "Facts have no snapshot revision."});
}
