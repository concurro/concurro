"""Role-selected feedback as verified runner files with a compact prompt index."""

from copy import deepcopy
import json
from pathlib import Path
import re

from policy import digest

FIELDS = {
    "design_feedback",
    "prior_reviews",
    "review_findings",
    "own_objections",
    "open_objections",
    "own_findings",
    "prior_outcomes_untrusted",
    "review_validation_feedback",
    "plan_validation_feedback",
    "intake_validation_feedback",
    "implementation_feedback",
}
INLINE = 8000
IDENTITIES = {"id", "status", "path", "head", "reviewer", "provider", "source_ids"}


def prepare(context):
    """Move lengthy selected feedback out of the prompt without selecting any additional evidence."""
    if "controller_feedback" in context:
        return context
    selected = {k: v for k, v in context.items() if k in FIELDS and v}
    if len(json.dumps(selected).encode()) <= INLINE:
        return context
    result, files = dict(context), {}
    for field, value in selected.items():
        if len(json.dumps(value).encode()) < 512:
            continue
        key = digest(value)
        files[key] = value
        result[field] = {
            "feedback_file": key + ".json",
            "digest": key,
            "index": index(value),
            "notice": "Index only; read the file for complete evidence.",
        }
    if files:
        result["controller_feedback"] = files
    return result


def index(value, field=None):
    """Preserve structure, identities and statuses while shortening narrative previews."""
    if isinstance(value, str):
        return value if field in IDENTITIES or len(value) <= 160 else value[:160] + " [continued in feedback file]"
    if isinstance(value, dict):
        return {k: index(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [index(v, field) for v in value]
    return value


def restore(context):
    """Recover selected feedback for stable call identity, not for the model prompt."""
    result = dict(context)
    files = result.pop("controller_feedback", {})
    for field in FIELDS & result.keys():
        value = result[field]
        if isinstance(value, dict) and value.get("digest") in files and "feedback_file" in value:
            result[field] = deepcopy(files[value["digest"]])
    return result


def materialize(files, directory):
    """Write only digest-named JSON files outside the checkout before starting the agent."""
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    for key, value in files.items():
        if not isinstance(key, str) or not re.fullmatch("[a-f0-9]{64}", key) or digest(value) != key:
            raise ValueError("Invalid feedback file commitment")
        path = directory / (key + ".json")
        with path.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
        path.chmod(0o400)
    return str(directory)
