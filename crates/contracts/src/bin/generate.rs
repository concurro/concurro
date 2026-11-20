//! Emit committed JSON Schemas for the registered wire contracts.

use std::{fs, path::PathBuf};

use anyhow::{Context, Result};
use concurro_contracts::REGISTRY;
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
    let mut manifest = Value::Array(manifest);
    sort_json_keys(&mut manifest);
    fs::write(
        contracts.join("registry.json"),
        format!("{}\n", serde_json::to_string_pretty(&manifest)?),
    )?;
    Ok(())
}
