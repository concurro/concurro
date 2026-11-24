//! Engine instruction bundle identity pinned into each task's runner payload.

use std::{
    collections::BTreeMap,
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

use anyhow::{Context, Result, ensure};
use concurro_tasks_core::InitializationSnapshot;
use serde_json::Value;
use sha2::{Digest, Sha256};

pub(crate) use concurro_tasks_core::{escape_non_ascii, python_digest, python_json};

/// Encode rejected output like Python's indented, ASCII-escaped JSON feedback.
pub(crate) fn python_pretty_json(value: &Value) -> Result<String> {
    Ok(escape_non_ascii(&serde_json::to_string_pretty(value)?))
}

/// One attempt's verified bundle and parsed controller wording.
pub struct ResolvedBundle {
    /// Exact resource bytes serialized into the attempt request.
    resources: BTreeMap<String, String>,
    /// Controller addenda from the same frozen resource set.
    controller: BTreeMap<String, String>,
}

impl ResolvedBundle {
    /// Resolve and verify one snapshot once, including legacy history when required.
    pub fn resolve(snapshot: &InitializationSnapshot) -> Result<Self> {
        Self::from_resources(snapshot_resources(snapshot)?)
    }

    /// Parse controller addenda once while retaining the exact bundle bytes.
    fn from_resources(resources: BTreeMap<String, String>) -> Result<Self> {
        let text = resources
            .get("bundle/controller.json")
            .map(String::as_str)
            .unwrap_or(include_str!(
                "../../../engine/resources/legacy-controller.json"
            ));
        let controller = serde_json::from_str(text)?;
        Ok(Self {
            resources,
            controller,
        })
    }

    /// Exact verified files serialized into this attempt's runner request.
    pub fn resources(&self) -> &BTreeMap<String, String> {
        &self.resources
    }

    /// Read a controller addendum from this attempt's resolved bundle.
    pub fn instruction(&self, key: &str) -> Result<&str> {
        self.controller
            .get(key)
            .map(String::as_str)
            .with_context(|| format!("missing controller instruction {key}"))
    }

    /// Keep task stage prompts on one repository snapshot and feedback contract.
    pub fn prompt_parts(
        &self,
        context: &Value,
        snapshot: &InitializationSnapshot,
        stage: &str,
        standalone_review: bool,
    ) -> Result<(Value, Value)> {
        ensure!(
            !standalone_review || stage == "review",
            "only a PR review can use standalone instructions"
        );
        let resource = |path: &str| {
            self.resources
                .get(path)
                .with_context(|| format!("missing bundle resource {path}"))
        };
        let mut untrusted = context
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("intake context is invalid"))?
            .clone();
        untrusted.retain(|key, _| !key.starts_with("controller_"));
        let authorization = untrusted.remove("design_authorization");
        untrusted.remove("charter");
        if standalone_review
            && untrusted.get("issue").and_then(|issue| issue.get("body"))
                == untrusted.get("published_pr_body")
        {
            untrusted
                .get_mut("issue")
                .and_then(Value::as_object_mut)
                .map(|issue| issue.remove("body"));
        }
        if stage == "plan" {
            for key in ["edit_policy", "limits"] {
                if untrusted.get(key) == untrusted["plan_output_contract"].get(key) {
                    untrusted.remove(key);
                }
            }
        }
        let repository = untrusted
            .remove("repository")
            .ok_or_else(|| anyhow::anyhow!("intake repository is missing"))?;
        let commit = untrusted
            .remove("repository_commit")
            .ok_or_else(|| anyhow::anyhow!("intake commit is missing"))?;
        let mut target = serde_json::Map::new();
        let role_name = match stage {
            "intake" => None,
            "plan" => Some("planner"),
            "plan_qc" => Some("design-reviewer"),
            "develop" => Some("implementer"),
            "review" => Some("reviewer"),
            _ => anyhow::bail!("unsupported task prompt stage"),
        };
        for (name, path) in [
            ("shared", Some(".concurro/instructions/shared.md")),
            (
                role_name.unwrap_or(""),
                role_name.map(|role| match role {
                    "planner" => ".concurro/instructions/planner.md",
                    "design-reviewer" => ".concurro/instructions/design-reviewer.md",
                    "reviewer" => ".concurro/instructions/reviewer.md",
                    _ => ".concurro/instructions/implementer.md",
                }),
            ),
        ] {
            if let Some(content) = path.and_then(|path| snapshot.instruction_texts.get(path)) {
                target.insert(name.to_owned(), Value::String(content.clone()));
            }
        }
        let charter: Value = serde_json::from_str(&snapshot.charter)?;
        let mut instructions = serde_json::json!({
            "shared": resource("bundle/instructions.md")?,
            "charter": charter,
            "role": stage,
            "skill": match stage {
                "plan" => resource("bundle/skills/concurro-design/SKILL.md")?,
                "plan_qc" => resource("bundle/skills/concurro-review-design/SKILL.md")?,
                "develop" => resource("bundle/skills/concurro-implement/SKILL.md")?,
                "review" => resource("bundle/skills/concurro-review-pr/SKILL.md")?,
                _ => resource("bundle/skills/concurro-assess-scope/SKILL.md")?,
            },
            "target": target,
        });
        if stage == "review" {
            instructions["mode"] = Value::String(
                if standalone_review {
                    resource("bundle/skills/concurro-review-pr/standalone.md")?
                } else {
                    resource("bundle/skills/concurro-review-pr/contribution.md")?
                }
                .to_owned(),
            );
        }
        let mut prompt = serde_json::json!({
            "instructions": instructions,
            "repository_snapshot": {
                "workspace": ".",
                "commit": commit,
                "refs": repository["refs"],
                "head": repository["head"],
                "local_checkpoint": null,
                "head_is_local_candidate": false,
            },
            "untrusted_context": untrusted,
        });
        if context.get("controller_feedback").is_some_and(|value| {
            !value.is_null() && value.as_object().is_some_and(|map| !map.is_empty())
        }) {
            prompt["feedback_files"] = serde_json::json!({
                "directory": "../feedback",
                "instruction": self.instruction("feedback_files")?,
            });
        }
        if let Some(authorization) = authorization {
            ensure!(
                stage == "develop",
                "implementation authorization entered an independent stage"
            );
            prompt["controller_verified_authorization"] = authorization;
        }
        if stage == "plan" {
            return Ok((
                prompt,
                serde_json::from_str(resource("bundle/schemas/plan.json")?)?,
            ));
        }
        if stage == "develop" {
            return Ok((
                prompt,
                serde_json::from_str(resource("bundle/schemas/develop.json")?)?,
            ));
        }
        let mut schema: Value = serde_json::from_str(match stage {
            "plan_qc" => resource("bundle/schemas/plan_qc.json")?,
            "review" => resource("bundle/schemas/review.json")?,
            _ => resource("bundle/schemas/intake.json")?,
        })?;
        if standalone_review {
            schema["properties"]["verdict"]["enum"] =
                serde_json::json!(["approve", "changes_requested"]);
        }
        if stage != "review" {
            let mut goals = charter["goals"]
                .as_object()
                .ok_or_else(|| anyhow::anyhow!("intake charter goals are invalid"))?
                .keys()
                .cloned()
                .collect::<Vec<_>>();
            goals.sort();
            schema["properties"]["scope"]["properties"]["goal_ids"]["items"]["enum"] =
                serde_json::to_value(goals)?;
        }
        let classification: Value =
            serde_json::from_str(resource("bundle/schemas/classification.json")?)?;
        schema["properties"]["classification"] = classification["schema"].clone();
        schema["required"]
            .as_array_mut()
            .ok_or_else(|| anyhow::anyhow!("intake response schema is invalid"))?
            .push(Value::String("classification".to_owned()));
        Ok((prompt, schema))
    }
}

/// Required resource paths; instruction contents are loaded from the runtime bundle.
const REQUIRED_RESOURCES: &[&str] = &[
    "bundle/instructions.md",
    "bundle/roles.json",
    "bundle/schemas/classification.json",
    "bundle/schemas/develop.json",
    "bundle/schemas/intake.json",
    "bundle/schemas/plan.json",
    "bundle/schemas/plan_qc.json",
    "bundle/schemas/review.json",
    "bundle/skills/concurro-assess-scope/SKILL.md",
    "bundle/skills/concurro-design/SKILL.md",
    "bundle/skills/concurro-implement/SKILL.md",
    "bundle/skills/concurro-review-design/SKILL.md",
    "bundle/skills/concurro-review-pr/SKILL.md",
    "bundle/skills/concurro-review-pr/contribution.md",
    "bundle/skills/concurro-review-pr/standalone.md",
];

/// Locate operator-owned bundle files independently of the process working directory.
fn bundle_directory() -> PathBuf {
    env::var_os("CONCURRO_TASKS_BUNDLE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("../../bundle"))
}

/// Validate resource paths and freeze the manifest and all referenced UTF-8 files.
fn read_resources(
    mut read: impl FnMut(&str) -> Result<String>,
) -> Result<BTreeMap<String, String>> {
    let text = read("bundle/manifest.json")?;
    let manifest: Value = serde_json::from_str(&text)?;
    ensure!(
        manifest["version"] == 1,
        "unsupported instruction bundle format"
    );
    let listed = manifest["resources"]
        .as_array()
        .context("instruction bundle resources are missing")?;
    let mut resources = BTreeMap::from([("bundle/manifest.json".to_owned(), text.clone())]);
    for path in listed {
        let path = path.as_str().context("invalid instruction bundle path")?;
        ensure!(
            REQUIRED_RESOURCES.contains(&path) || path == "bundle/controller.json",
            "unknown instruction bundle resource: {path}"
        );
        ensure!(
            !resources.contains_key(path),
            "duplicate instruction bundle resource: {path}"
        );
        resources.insert(path.to_owned(), read(path)?);
    }
    ensure!(
        REQUIRED_RESOURCES
            .iter()
            .all(|path| resources.contains_key(*path)),
        "instruction bundle is incomplete"
    );
    Ok(resources)
}

/// Read the current operator bundle once for a new initialization or explicit adoption.
pub fn load_bundle_resources() -> Result<BTreeMap<String, String>> {
    let directory = bundle_directory();
    read_resources(|path| {
        fs::read_to_string(
            directory.join(
                path.strip_prefix("bundle/")
                    .context("invalid resource path")?,
            ),
        )
        .with_context(|| format!("read runtime bundle {path}"))
    })
}

/// Recover legacy snapshots only from bytes whose complete identity matches the recorded pin.
pub(crate) fn snapshot_resources(
    snapshot: &InitializationSnapshot,
) -> Result<BTreeMap<String, String>> {
    if !snapshot.engine_resources.is_empty() {
        ensure!(
            identity(
                &snapshot.charter,
                &snapshot.instruction_texts,
                &snapshot.engine_resources
            )? == snapshot.bundle_id,
            "stored prompt bundle identity mismatch"
        );
        return Ok(snapshot.engine_resources.clone());
    }
    let current = load_bundle_resources()?;
    if identity(&snapshot.charter, &snapshot.instruction_texts, &current)? == snapshot.bundle_id {
        return Ok(current);
    }
    let configured_root = bundle_directory()
        .parent()
        .context("bundle directory has no parent")?
        .to_owned();
    let packaged_root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for root in [configured_root, packaged_root] {
        let history = Command::new("git")
            .arg("-C")
            .arg(&root)
            .args(["log", "--format=%H", "--", "bundle"])
            .output()?;
        for revision in String::from_utf8(history.stdout)?.lines() {
            let resources = read_resources(|path| {
                let shown = Command::new("git")
                    .arg("-C")
                    .arg(&root)
                    .args(["show", &format!("{revision}:{path}")])
                    .output()?;
                ensure!(
                    shown.status.success(),
                    "historical bundle resource is unavailable"
                );
                Ok(String::from_utf8(shown.stdout)?)
            });
            if let Ok(resources) = resources
                && identity(&snapshot.charter, &snapshot.instruction_texts, &resources)?
                    == snapshot.bundle_id
            {
                return Ok(resources);
            }
        }
    }
    anyhow::bail!(
        "Pinned prompt bundle {} is unavailable; restore its bundle files/history or use an authorized resume to adopt the current bundle",
        snapshot.bundle_id
    )
}

pub(crate) const TARGET_INSTRUCTIONS: [&str; 5] = [
    ".concurro/instructions/shared.md",
    ".concurro/instructions/planner.md",
    ".concurro/instructions/design-reviewer.md",
    ".concurro/instructions/implementer.md",
    ".concurro/instructions/reviewer.md",
];

/// Hash the current runtime bundle with the target charter and instruction bytes.
pub fn bundle_identity(charter: &str, instructions: &BTreeMap<String, String>) -> Result<String> {
    identity(charter, instructions, &load_bundle_resources()?)
}

/// Hash exact bundle bytes with the target guidance frozen in an initialization.
pub(crate) fn identity(
    charter: &str,
    instructions: &BTreeMap<String, String>,
    resources: &BTreeMap<String, String>,
) -> Result<String> {
    ensure!(
        instructions
            .keys()
            .all(|path| TARGET_INSTRUCTIONS.contains(&path.as_str())),
        "target instructions contain an unapproved path"
    );
    let charter_value: Value =
        serde_json::from_str(charter).map_err(|_| anyhow::anyhow!("project charter is invalid"))?;
    ensure!(
        charter_value["version"] == 1
            && ["id", "goals", "non_goals", "constraints", "acceptance"]
                .into_iter()
                .all(|key| match &charter_value[key] {
                    Value::Null | Value::Bool(false) => false,
                    Value::Number(number) => number.as_f64() != Some(0.0),
                    Value::String(text) => !text.is_empty(),
                    Value::Array(items) => !items.is_empty(),
                    Value::Object(items) => !items.is_empty(),
                    Value::Bool(true) => true,
                })
            && charter_value["goals"]
                .as_object()
                .is_some_and(|goals| goals.keys().all(|key| !key.trim().is_empty())),
        "project charter is incomplete"
    );
    let mut hashes = BTreeMap::new();
    for (path, content) in resources {
        hashes.insert(
            path.clone(),
            hex::encode(Sha256::digest(content.as_bytes())),
        );
    }
    hashes.insert(
        ".concurro/charter.json".to_owned(),
        hex::encode(Sha256::digest(charter.as_bytes())),
    );
    for (path, content) in instructions {
        hashes.insert(
            path.clone(),
            hex::encode(Sha256::digest(content.as_bytes())),
        );
    }
    python_digest(&serde_json::to_value(hashes)?)
}

/// Prove a changed bundle differs from the approved one only in the implementer skill.
pub(crate) fn implementer_skill_update(
    prior: &InitializationSnapshot,
    current: &InitializationSnapshot,
) -> Result<Option<(String, String)>> {
    if prior.bundle_id == current.bundle_id
        || prior.charter != current.charter
        || prior.instruction_texts != current.instruction_texts
    {
        return Ok(None);
    }
    let old = snapshot_resources(prior)?;
    let mut new = snapshot_resources(current)?;
    let path = "bundle/skills/concurro-implement/SKILL.md";
    let old_skill = old.get(path).context("missing prior implementer skill")?;
    let new_skill = new
        .insert(path.to_owned(), old_skill.clone())
        .context("missing current implementer skill")?;
    if old == new && old_skill != &new_skill {
        return Ok(Some((
            hex::encode(Sha256::digest(old_skill.as_bytes())),
            hex::encode(Sha256::digest(new_skill.as_bytes())),
        )));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use std::{
        collections::BTreeMap,
        env, fs,
        io::Write,
        process::{Command, Stdio},
    };

    use concurro_tasks_core::{InitializationSnapshot, PinnedInputs, Requirements};
    use serde_json::json;
    use sha2::{Digest, Sha256};
    use uuid::Uuid;

    use super::{
        ResolvedBundle, bundle_identity, identity, load_bundle_resources, python_digest,
        python_pretty_json,
    };

    /// Match the retained Python prompt and schema for every M2 design role.
    #[test]
    fn stage_prompts_match_python_bundle() {
        let charter = r#"{"version":1,"id":"test","goals":{"G1":"Correctness"},"non_goals":["n"],"constraints":["c"],"acceptance":["a"]}"#;
        let instruction_texts = BTreeMap::from([
            (
                ".concurro/instructions/shared.md".to_owned(),
                "Shared target".to_owned(),
            ),
            (
                ".concurro/instructions/planner.md".to_owned(),
                "Plan target".to_owned(),
            ),
            (
                ".concurro/instructions/design-reviewer.md".to_owned(),
                "Review target".to_owned(),
            ),
            (
                ".concurro/instructions/implementer.md".to_owned(),
                "Implement target".to_owned(),
            ),
            (
                ".concurro/instructions/reviewer.md".to_owned(),
                "PR review target".to_owned(),
            ),
        ]);
        let snapshot = InitializationSnapshot {
            pins: PinnedInputs {
                config_sha: "a".repeat(40),
                base_sha: "b".repeat(40),
                max_model_calls: 8,
                max_fix_rounds: 1,
                max_plan_revisions: 1,
            },
            configuration: json!({}),
            requirements: Requirements {
                title: "Task".to_owned(),
                body: None,
                discussion: vec![],
            },
            charter: charter.to_owned(),
            instruction_texts: instruction_texts.clone(),
            policy_documents: BTreeMap::new(),
            base_files: vec![],
            engine_resources: BTreeMap::new(),
            bundle_id: bundle_identity(charter, &instruction_texts).expect("bundle"),
        };
        let repository = json!({"refs":{"main":"b".repeat(40)},"head":"main"});
        for (stage, kind) in [
            ("plan", "contribution"),
            ("plan_qc", "contribution"),
            ("develop", "contribution"),
            ("review", "contribution"),
            ("review", "standalone_pr_review"),
        ] {
            let standalone = kind == "standalone_pr_review";
            let mut context = json!({
                "repository":repository,"repository_commit":"b".repeat(40),
                "charter":serde_json::from_str::<serde_json::Value>(charter).expect("charter"),
                "issue":{"title":"Task","body":"Build it","discussion":[]},
                "edit_policy":{"allow":["src/**"]},"limits":{"primary_files":4},
                "plan_output_contract":{"edit_policy":{"allow":["src/**"]},"limits":{"primary_files":4}},
                "reviewer_identity":{"id":"reviewer-1"},
                "controller_secret":"omitted",
            });
            if standalone {
                context["published_pr_body"] = json!("Build it");
            }
            if stage == "develop" {
                context["design_authorization"] =
                    json!({"verified_by":"controller","binding":"exact"});
            }
            let (rust_prompt, rust_schema) = ResolvedBundle::resolve(&snapshot)
                .expect("resolved bundle")
                .prompt_parts(&context, &snapshot, stage, standalone)
                .expect("Rust prompt");
            let input = json!({
                "stage":stage,"kind":kind,"context":context,"charter":charter,
                "instruction_texts":instruction_texts,
            });
            let script = r#"import json,sys
sys.path.insert(0,'engine')
import bundle,prompts
data=json.load(sys.stdin)
frozen=bundle.Bundle(root='.',charter_text=data['charter'],instruction_texts=data['instruction_texts'])
request={'stage':data['stage'],'task_kind':data['kind'],'context':data['context'],'local_candidate':False}
print(json.dumps({'prompt':json.loads(prompts.prompt_for(request,frozen)),'schema':frozen.schema(data['stage'],data['kind'])}))
"#;
            let mut python = Command::new("python3")
                .args(["-c", script])
                .current_dir(env!("CARGO_MANIFEST_DIR").to_owned() + "/../..")
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .expect("Python oracle");
            python
                .stdin
                .take()
                .expect("stdin")
                .write_all(&serde_json::to_vec(&input).expect("oracle input"))
                .expect("write oracle input");
            let output = python.wait_with_output().expect("Python result");
            assert!(
                output.status.success(),
                "Python oracle: {}",
                String::from_utf8_lossy(&output.stderr)
            );
            let oracle: serde_json::Value =
                serde_json::from_slice(&output.stdout).expect("oracle JSON");
            assert_eq!(rust_prompt, oracle["prompt"], "{stage} prompt");
            assert_eq!(rust_schema, oracle["schema"], "{stage} schema");
        }
    }

    #[test]
    fn bundle_identity_matches_python_bundle() {
        let charter = "{\"version\":1,\"id\":\"test\",\"goals\":{\"g\":\"v\"},\"non_goals\":[\"n\"],\"constraints\":[\"c\"],\"acceptance\":[\"a\"]}";
        assert_eq!(
            bundle_identity(charter, &BTreeMap::new()).expect("bundle"),
            "75d1da5acf6a7f411effc0b6ef2df538b50349f4c60014db1a56ab85f6ca3906"
        );
        assert_ne!(
            {
                let mut resources = load_bundle_resources().expect("resources");
                resources.insert(
                    "bundle/skills/concurro-implement/SKILL.md".to_owned(),
                    "older implementer skill".to_owned(),
                );
                identity(charter, &BTreeMap::new(), &resources)
            }
            .expect("historical bundle"),
            bundle_identity(charter, &BTreeMap::new()).expect("current bundle"),
        );
    }

    /// Changed runtime files affect new snapshots while existing tasks keep their exact bytes.
    #[test]
    fn runtime_bundle_updates_preserve_frozen_snapshots_and_skill_retry_proofs() {
        let root = env::temp_dir().join(format!("concurro-runtime-bundle-{}", Uuid::now_v7()));
        let resources = load_bundle_resources().expect("runtime resources");
        for (path, text) in &resources {
            let path = root.join(path);
            fs::create_dir_all(path.parent().expect("parent")).expect("directories");
            fs::write(path, text).expect("resource");
        }
        let load = || {
            super::read_resources(|path| Ok(fs::read_to_string(root.join(path))?))
                .expect("read files")
        };
        let charter = r#"{"version":1,"id":"test","goals":{"G1":"Goal"},"non_goals":["n"],"constraints":["c"],"acceptance":["a"]}"#;
        let old = InitializationSnapshot {
            pins: PinnedInputs {
                config_sha: "a".repeat(40),
                base_sha: "b".repeat(40),
                max_model_calls: 8,
                max_fix_rounds: 2,
                max_plan_revisions: 2,
            },
            configuration: json!({}),
            requirements: Requirements {
                title: "Task".to_owned(),
                body: None,
                discussion: vec![],
            },
            charter: charter.to_owned(),
            instruction_texts: BTreeMap::new(),
            policy_documents: BTreeMap::new(),
            base_files: vec![],
            engine_resources: load(),
            bundle_id: identity(charter, &BTreeMap::new(), &resources).expect("identity"),
        };
        let frozen: InitializationSnapshot =
            serde_json::from_slice(&serde_json::to_vec(&old).expect("snapshot bytes"))
                .expect("restart snapshot");
        let skill = "bundle/skills/concurro-implement/SKILL.md";
        fs::write(root.join(skill), "Updated implementer guidance.").expect("change runtime skill");
        let mut current = old.clone();
        current.engine_resources = load();
        current.bundle_id =
            identity(charter, &BTreeMap::new(), &current.engine_resources).expect("new identity");
        assert_ne!(current.bundle_id, frozen.bundle_id);
        assert!(
            super::implementer_skill_update(&frozen, &current)
                .expect("proof")
                .is_some()
        );
        let context =
            json!({"repository":{"refs":{},"head":"main"},"repository_commit":"b".repeat(40)});
        assert_eq!(
            ResolvedBundle::resolve(&frozen)
                .expect("resolved bundle")
                .prompt_parts(&context, &frozen, "develop", false)
                .expect("frozen prompt")
                .0["instructions"]["skill"],
            resources[skill]
        );
        assert_eq!(
            ResolvedBundle::resolve(&current)
                .expect("resolved bundle")
                .prompt_parts(&context, &current, "develop", false)
                .expect("new prompt")
                .0["instructions"]["skill"],
            "Updated implementer guidance."
        );
        fs::write(root.join("bundle/instructions.md"), "New shared guidance.")
            .expect("change runtime shared instructions");
        current.engine_resources = load();
        current.bundle_id =
            identity(charter, &BTreeMap::new(), &current.engine_resources).expect("identity");
        assert!(
            super::implementer_skill_update(&frozen, &current)
                .expect("non-skill change")
                .is_none()
        );
        assert_eq!(
            ResolvedBundle::resolve(&frozen)
                .expect("resolved bundle")
                .prompt_parts(&context, &frozen, "intake", false)
                .expect("old prompt after update")
                .0["instructions"]["shared"],
            resources["bundle/instructions.md"]
        );
        let historical = super::read_resources(|path| {
            let shown = Command::new("git")
                .args(["show", &format!("HEAD:{path}")])
                .current_dir(env!("CARGO_MANIFEST_DIR").to_owned() + "/../..")
                .output()?;
            anyhow::ensure!(shown.status.success(), "history resource");
            Ok(String::from_utf8(shown.stdout)?)
        })
        .expect("historical bundle");
        let mut legacy = frozen.clone();
        legacy.engine_resources.clear();
        legacy.bundle_id =
            identity(charter, &BTreeMap::new(), &historical).expect("legacy identity");
        let resolved =
            ResolvedBundle::resolve(&legacy).expect("recover exact historical bundle once");
        assert_eq!(resolved.resources(), &historical);
        assert!(
            !super::bundle_directory()
                .join("legacy-controller.json")
                .exists()
        );
        assert_eq!(
            hex::encode(Sha256::digest(include_bytes!(
                "../../../engine/resources/legacy-controller.json"
            ))),
            "22ab6a49b333684f2d82c992d653ab705105180f57a49d7dbb651e38d6c94952"
        );
        assert!(
            resolved
                .instruction("intake_correction")
                .expect("pinned legacy helper wording")
                .contains("advisory concerns")
        );
        assert!(
            resolved
                .prompt_parts(&context, &legacy, "intake", false)
                .expect("legacy prompt")
                .1["properties"]
                .get("concerns")
                .is_some()
        );
        let rendered = resolved
            .prompt_parts(&context, &legacy, "intake", false)
            .expect("legacy rendered prompt");
        legacy.bundle_id = "missing-bundle".to_owned();
        assert_eq!(
            resolved
                .prompt_parts(&context, &legacy, "intake", false)
                .expect("render uses the resolved object, without another history lookup"),
            rendered
        );
        assert!(
            resolved
                .instruction("intake_correction")
                .expect("lookup after resolution")
                .contains("advisory concerns")
        );
        assert_eq!(resolved.resources(), &historical);
        assert!(
            super::snapshot_resources(&legacy)
                .expect_err("unavailable provenance")
                .to_string()
                .contains("authorized resume")
        );
        fs::remove_dir_all(root).expect("cleanup");
    }

    #[test]
    fn python_digest_escapes_non_ascii_text_and_sorts_nested_keys() {
        assert_eq!(
            python_digest(&json!({"z": ["😀", {"b": 1, "a": "é"}], "a": true})).expect("digest"),
            "8763d15bb81d0ff57b5f6e16d1d2cfa0b3f69c8f29872a661d4fdb94a7243cdd"
        );
    }

    #[test]
    fn correction_feedback_uses_ascii_escaped_indented_json() {
        assert_eq!(
            python_pretty_json(&json!({"answer": "réponse 😀"})).expect("feedback"),
            "{\n  \"answer\": \"r\\u00e9ponse \\ud83d\\ude00\"\n}"
        );
    }
}
