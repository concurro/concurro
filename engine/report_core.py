"""Dependency-free projection of authoritative report fields."""


def core(report):
    """Project only the advisory classification field away; preserve all core fields."""
    return {k: v for k, v in report.items() if k != "classification"} if isinstance(report, dict) else report
