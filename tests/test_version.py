import tomllib
from pathlib import Path

import email_export_import


def test_version_present_and_matches_pyproject():
    assert isinstance(email_export_import.__version__, str)
    assert email_export_import.__version__
    data = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert email_export_import.__version__ == data["project"]["version"]
