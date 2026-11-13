# Exact-parent persistence fixtures

`manifest.json` records source commits and raw/compressed hashes. `generate.py` invokes
that exported parent's real Registry and checkpoint writers using existing synthetic GitHub
fixtures; main also writes a real persistent issue cache. No provider or credential access
occurs. Gzip keeps the detached-report examples compact; timestamps and immutable IDs remain
as written. Tests distinguish wire versions from normalized in-memory version 2.

```bash
python3 generate.py /absolute/exported-parent /absolute/new-output FULL_PARENT_SHA
```

Export the exact parent tree first. Outputs are new directories; compress the recorded files
with gzip `mtime=0` and retain both raw and compressed SHA256 identities in the manifest.
