//! Emit committed JSON Schemas for the registered wire contracts.

use std::{fs, path::PathBuf};

use anyhow::{Context, Result};
use concurro_contracts::{
    Admission, AdmissionRequest, BlobRef, CommandClaims, CommandSubmission, ExchangeSessionClaim,
    FinishRequest, Grant, ProbeResponse, REGISTRY, RetiredSessions, RunnerClaimReply,
    RunnerKeyReply, RunnerKeyRequest, RunnerNextTurn, SessionStatusRequest, SessionStatusResponse,
    TurnObservation, TurnPayloadV2, TurnResultV2, TurnSubmissionV2,
};
use schemars::generate::SchemaSettings;
use serde_json::{Value, json};

/// Keep committed schema ordering stable when another crate preserves JSON source order.
fn sort_json_keys(value: &mut Value) {
    match value {
        Value::Object(fields) => {
            for nested in fields.values_mut() {
                sort_json_keys(nested);
            }
            fields.sort_keys();
        }
        Value::Array(values) => {
            for nested in values {
                sort_json_keys(nested);
            }
        }
        _ => {}
    }
}

/// Generate both wire directions and the Python generator's registry manifest.
fn main() -> Result<()> {
    let contracts = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../contracts");
    let schemas = contracts.join("schemas");
    fs::create_dir_all(&schemas).with_context(|| format!("create {}", schemas.display()))?;
    let mut manifest = Vec::new();
    for registration in REGISTRY {
        let stem = format!("{}.v{}", registration.family, registration.version);
        manifest.push(json!({
            "family": registration.family,
            "version": registration.version,
            "stem": stem,
            "python_module": stem.replace('.', "_"),
            "python_decoder": registration.python_decoder,
        }));
        for (suffix, settings) in [
            ("de", SchemaSettings::draft2020_12().for_deserialize()),
            ("ser", SchemaSettings::draft2020_12().for_serialize()),
        ] {
            let mut schema = (registration.schema)(settings)?;
            (registration.prepare_schema)(&mut schema);
            sort_json_keys(&mut schema);
            let output = schemas.join(format!("{stem}.{suffix}.json"));
            fs::write(
                &output,
                format!("{}\n", serde_json::to_string_pretty(&schema)?),
            )
            .with_context(|| format!("write {}", output.display()))?;
        }
    }
    for (name, schema) in [
        (
            "command.submission.v1.de",
            SchemaSettings::draft2020_12()
                .for_deserialize()
                .into_generator()
                .into_root_schema_for::<CommandSubmission>(),
        ),
        (
            "command.claims.v1.de",
            SchemaSettings::draft2020_12()
                .for_deserialize()
                .into_generator()
                .into_root_schema_for::<CommandClaims>(),
        ),
        (
            "command.submission.v1.ser",
            SchemaSettings::draft2020_12()
                .for_serialize()
                .into_generator()
                .into_root_schema_for::<CommandSubmission>(),
        ),
        (
            "command.claims.v1.ser",
            SchemaSettings::draft2020_12()
                .for_serialize()
                .into_generator()
                .into_root_schema_for::<CommandClaims>(),
        ),
    ] {
        let mut schema = serde_json::to_value(schema)?;
        sort_json_keys(&mut schema);
        fs::write(
            schemas.join(format!("{name}.json")),
            format!("{}\n", serde_json::to_string_pretty(&schema)?),
        )?;
    }
    let mut api_manifest = Vec::new();
    /// Keep API schema and Python model names driven by one typed registration list.
    macro_rules! api_schema {
        ($stem:literal, $type:ty) => {{
            let stem = $stem;
            api_manifest.push(json!({"stem":stem,"python_module":stem.replace('.', "_")}));
            for (suffix, settings) in [
                ("de", SchemaSettings::draft2020_12().for_deserialize()),
                ("ser", SchemaSettings::draft2020_12().for_serialize()),
            ] {
                let mut schema = serde_json::to_value(settings.into_generator().into_root_schema_for::<$type>())?;
                if let Some(payer) = schema.pointer_mut("/$defs/RequestActor") {
                    payer["additionalProperties"] = json!(false);
                }
                sort_json_keys(&mut schema);
                fs::write(schemas.join(format!("{stem}.{suffix}.json")),
                    format!("{}\n", serde_json::to_string_pretty(&schema)?))?;
            }
        }};
    }
    api_schema!("exchange.operator.submit.v2", TurnSubmissionV2);
    api_schema!("exchange.operator.observation.v1", TurnObservation);
    api_schema!("exchange.operator.retirement.v1", RetiredSessions);
    api_schema!("exchange.operator.grant.v1", Grant);
    api_schema!("exchange.admission.request.v1", AdmissionRequest);
    api_schema!("exchange.admission.admitted.v1", Admission);
    api_schema!("exchange.admission.probe.v1", ProbeResponse);
    api_schema!("exchange.admission.finish.v1", FinishRequest);
    api_schema!(
        "exchange.admission.sessions_request.v1",
        SessionStatusRequest
    );
    api_schema!(
        "exchange.admission.sessions_response.v1",
        SessionStatusResponse
    );
    api_schema!("exchange.runner.payload.v2", TurnPayloadV2);
    api_schema!("exchange.runner.result.v2", TurnResultV2);
    api_schema!("exchange.runner.blob.v2", BlobRef);
    api_schema!("exchange.runner.claim_reply.v2", RunnerClaimReply);
    api_schema!("exchange.runner.key_request.v2", RunnerKeyRequest);
    api_schema!("exchange.runner.key_reply.v2", RunnerKeyReply);
    api_schema!("exchange.runner.next_turn.v2", RunnerNextTurn);
    api_schema!("exchange.relay.session_claim.v1", ExchangeSessionClaim);
    let mut api_manifest = Value::Array(api_manifest);
    sort_json_keys(&mut api_manifest);
    fs::write(
        contracts.join("api-registry.json"),
        format!("{}\n", serde_json::to_string_pretty(&api_manifest)?),
    )?;
    let mut manifest = Value::Array(manifest);
    sort_json_keys(&mut manifest);
    fs::write(
        contracts.join("registry.json"),
        format!("{}\n", serde_json::to_string_pretty(&manifest)?),
    )?;
    Ok(())
}
