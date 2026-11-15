"""Relative directories in a native runner turn, shared with prompt construction."""

# One turn directory per job: provider transcripts and target caches are keyed by its path.
TURN = "turn-" + "0" * 32
WORKSPACE = "work"
FEEDBACK = "feedback"
