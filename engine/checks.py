"""Executable check registry and the matching capability contract for every provider."""

COMMANDS = {
    "format": ["cargo", "fmt", "--all", "--", "--check"],
    "check": ["cargo", "check", "--workspace", "--locked", "--offline"],
    "nextest": ["cargo", "nextest", "run", "--workspace", "--lib", "--locked", "--offline"],
}
DESCRIPTIONS = {
    "diff": "Whitespace validation of the complete checkpoint diff against the synthetic local baseline; requires a clean worktree.",
    "scope": "Require exactly the declared changed paths and addition/modification status against approved original contents, including across repair attempts; reject empty diffs and uncommitted files.",
    "text": "Check each declared file's optional line limit and required literal text in the checkpoint.",
    "format": "Run cargo fmt for the workspace without modifying files.",
    "check": "Compile the workspace offline using prepared dependencies.",
    "nextest": "Run workspace library tests offline using prepared dependencies.",
}
CONTROL_CHECKS = ("diff", "scope", "text")
REPAIR_DETAILS = {
    'validation_shape': 'Return a validation object containing checks and files.',
    'validation_checks': 'Select distinct available check IDs and include every mandatory check for the planned paths.',
    'validation_files': 'Declare exactly the planned paths with add/modify status, an optional positive line limit and bounded literal text assertions.',
    'validation_change': 'The declared add/modify status disagrees with file existence at the approved source base.',
}


def required(paths):
    """Select mandatory checks from candidate file types."""
    return list(CONTROL_CHECKS) + (list(COMMANDS) if any(p.endswith('.rs') for p in paths) else [])


def available(prepared):
    """Expose dependency-consuming checks only when Cargo preparation is required."""
    return [*CONTROL_CHECKS, "format"] + (["check", "nextest"] if {"check", "nextest"} & set(prepared) else [])


def catalog(paths=()):
    """Describe capabilities from the same identifiers used by execution dispatch."""
    return {"version": 1, "checks": [{"id": name, "purpose": DESCRIPTIONS[name]}
                                      for name in available(required(paths))],
            "mandatory": required(paths), "rust_requires": list(COMMANDS),
            "baseline": "Archive content is imported into a new local Git repository. The remote commit SHA is provenance, not a usable local Git revision.",
            "execution": "Request named checks and file rules, never shell commands. The controller checkpoints all edits before checks; inspection exposes read-only file tools and no Git history.",
            "limits": "Named checks do not prove prose accuracy or requirement coverage. Reviewers must assess those against source evidence."}


def contract_errors(plan):
    """Return fixed repair codes for check selections and declarative file rules."""
    spec = plan.get('validation')
    if not isinstance(spec, dict) or set(spec) != {'checks', 'files'}:
        return ['validation_shape']
    names, rules = spec['checks'], spec['files']
    if (not isinstance(names, list) or not all(isinstance(n, str) for n in names)
            or len(names) != len(set(names)) or not set(names).issubset(available(required(plan['files'])))
            or not set(required(plan['files'])).issubset(names)):
        return ['validation_checks']
    if not isinstance(rules, list) or len(rules) != len(plan['files']):
        return ['validation_files']
    paths = []
    for rule in rules:
        if (not isinstance(rule, dict) or set(rule) != {'path', 'change', 'max_lines', 'required_text'}
                or not isinstance(rule['path'], str) or rule['change'] not in ('add', 'modify')
                or rule['max_lines'] is not None and (type(rule['max_lines']) is not int or not 1 <= rule['max_lines'] <= 100000)
                or not isinstance(rule['required_text'], list) or len(rule['required_text']) > 16
                or not all(isinstance(t, str) and 0 < len(t) <= 300 for t in rule['required_text'])):
            return ['validation_files']
        paths.append(rule['path'])
    if len(set(paths)) != len(paths) or set(paths) != set(plan['files']):
        return ['validation_files']
    return []
