"""Dependency-free GitHub URL and protocol vocabulary shared by engine modules."""

# Comment and PR bodies retain their existing allowance.
COMMENT_BODY_BYTES = 60000
PUBLICATION_BODY_BYTES = COMMENT_BODY_BYTES  # Existing publication callers.
# Human-readable artifact views remain compact independently of data-part sizing.
ARTIFACT_VIEW_BYTES = 60000
# Storage granularity, not a limit on total evidence; immutable records may span parts.
ARTIFACT_BODY_BYTES = 256 * 1024
# Mutable audit versions repeat in GitHub responses; spill excess metadata into immutable data.
AUDIT_TARGET_BYTES = 60000

EVENT_MARKER = "<!-- concurro:event:v1:"
DIGEST_PATTERN = r"[a-f0-9]{64}"
SHA_PATTERN = r"[0-9a-f]{40}"
COMMENT_URL = "https://github.com/{repository}/issues/{issue}#issuecomment-{comment}"


def issue_url(repository, issue):
    """Return the public issue URL without escaping or normalizing its components."""
    return f"https://github.com/{repository}/issues/{issue}"


def artifact_url(repository, issue, number=None):
    """Return an issue's artifact root, or a specific artifact when a number is supplied."""
    root = issue_url(repository, issue) + "/artifacts/"
    return root if number is None else root + str(number)


def comment_url(repository, issue, comment_id):
    """Return an issue-comment permalink using the existing named-format template."""
    return COMMENT_URL.format(repository=repository, issue=issue, comment=comment_id)


def clone_url(repository):
    """Return the public HTTPS Git clone URL without modifying the repository name."""
    return f"https://github.com/{repository}.git"
