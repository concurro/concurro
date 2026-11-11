"""Artifact response accounting; raw JSON bytes are exact, rendered HTML is an allowance."""

import json

import protocol

# The captured latest-only audit response has 730 bytes outside its four body strings.
# Reserve 8 KiB for actors, names, URLs and envelopes, independently of payload size.
METADATA_BYTES = 8192
# Live JSON-fence rendering reached 4.70x; allow 8x escaped visible text plus fixed markup.
# This is calibrated headroom, not a bound on arbitrary future GitHub rendering.
HTML_MULTIPLIER = 8
HTML_OVERHEAD = 1024


class CapacityError(ValueError):
    """A frozen artifact cannot be read under the configured response allowance."""


def json_bytes(value):
    """Count compact UTF-8 JSON bytes, matching the captured GitHub response serialization."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def response_sizes(body, readable, *, historical=None):
    """Estimate POST, one-item listing and detail reads; history can carry a different old body/view."""
    contribution = json_bytes(body) + HTML_MULTIPLIER * json_bytes(readable) + HTML_OVERHEAD
    previous = contribution
    if historical is not None:
        old_body, old_view = historical
        previous = json_bytes(old_body) + HTML_MULTIPLIER * json_bytes(old_view) + HTML_OVERHEAD
    return {
        "post": METADATA_BYTES + contribution,
        "listing": METADATA_BYTES + contribution,
        "detail": METADATA_BYTES + contribution + previous,
    }


def fits(body, readable):
    """Check both GitHub's independent body ceiling and the shared transport allowance."""
    return (
        len(body.encode()) <= protocol.GITHUB_ARTIFACT_BODY_BYTES
        and max(response_sizes(body, readable).values()) <= protocol.ARTIFACT_RESPONSE_BYTES
    )
