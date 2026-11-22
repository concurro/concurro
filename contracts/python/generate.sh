#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
generator="${DATAMODEL_CODEGEN:-$root/contracts/python/.venv/bin/datamodel-codegen}"
mkdir -p "$root/contracts/python/concurro_contracts"
while IFS=$'\t' read -r stem module; do
  output="$root/contracts/python/concurro_contracts/$module.py"
  "$generator" \
    --input "$root/contracts/schemas/$stem.de.json" \
    --input-file-type jsonschema \
    --output "$output" \
    --output-model-type pydantic_v2.BaseModel \
    --target-python-version 3.12 \
    --target-pydantic-version 2.12 \
    --strict-types str bytes int float bool \
    --base-class concurro_contracts.strict.StrictModel \
    --formatters builtin \
    --disable-timestamp \
    --use-field-description
  python3 "$root/contracts/python/strict_roots.py" "$output"
done < <(python3 -c 'import json,sys; [print(item["stem"] + "\t" + item["python_module"]) for path in sys.argv[1:] for item in json.load(open(path))]' "$root/contracts/registry.json" "$root/contracts/api-registry.json")
