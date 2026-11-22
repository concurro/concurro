"""Use strict root bases where datamodel-code-generator emits RootModel."""

import sys
from pathlib import Path


def main():
    """Rewrite generated root bases without changing schema-derived fields."""
    path = Path(sys.argv[1])
    source = path.read_text()
    assert "from concurro_contracts.strict import StrictModel" in source
    if "RootModel[" not in source:
        return
    source = source.replace(
        "from concurro_contracts.strict import StrictModel",
        "from concurro_contracts.strict import StrictModel, StrictRootModel, StrictTimestamp",
    ).replace("RootModel[", "StrictRootModel[")
    source = source.replace("class Timestamp(StrictRootModel[AwareDatetime]):", "class Timestamp(StrictTimestamp):")
    source = source.replace(
        "class Timestamp(StrictTimestamp):\n    root: AwareDatetime", "class Timestamp(StrictTimestamp):\n    root: str"
    )
    source = source.replace("    RootModel,\n", "")
    path.write_text(source)


if __name__ == "__main__":
    main()
