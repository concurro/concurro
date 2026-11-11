"""Historical fixed-width shared layouts for compatibility and recovery tests."""

from unittest.mock import patch

from policy import digest


def fixed_parts(width):
    """Freeze a phase-two renderer/layout without reinstating a production part-size setting."""

    def pack(key, encoded, view, blocks, repository, issue):
        """Use the old explicit character boundaries, independent of current response packing."""
        return {
            "version": 2,
            "renderer": 1,
            "ends": list(range(width, len(encoded), width)) + [len(encoded)],
            "record": digest(encoded),
            "view": digest(view),
            "blocks": blocks,
        }

    return patch("report_artifact.pack", side_effect=pack)
