//! Emit committed JSON Schemas for the registered wire contracts.

use std::{fs, path::PathBuf};

use anyhow::{Context, Result};
use concurro_contracts::REGISTRY;
use schemars::generate::SchemaSettings;
use serde_json::json;

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
            let output = schemas.join(format!("{stem}.{suffix}.json"));
            fs::write(
                &output,
                format!("{}\n", serde_json::to_string_pretty(&schema)?),
            )
            .with_context(|| format!("write {}", output.display()))?;
        }
    }
    fs::write(
        contracts.join("registry.json"),
        format!("{}\n", serde_json::to_string_pretty(&manifest)?),
    )?;
    Ok(())
}
