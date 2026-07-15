"""Guard against the Windows-only crash class where a text file is read or
written without an explicit encoding, so Python uses the platform default
(cp1252 on Windows) and chokes on the Turkish locale / folder names.

Every read_text/write_text/fdopen(text) in the package must pass encoding=.
"""
import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "email_export_import"

# Text file operations that must carry an explicit encoding.
PATTERNS = [
    re.compile(r"\.read_text\("),
    re.compile(r"\.write_text\("),
    re.compile(r"os\.fdopen\([^)]*['\"][rwa]"),  # text-mode fdopen
]


def _calls_missing_encoding(source: str):
    """Yield (lineno, snippet) for a matched call whose argument span has no
    encoding=. The span is the call's parenthesised arguments, which may run
    across several lines."""
    for pat in PATTERNS:
        for m in pat.finditer(source):
            # capture up to the matching close paren (balanced, simple scan)
            i = source.index("(", m.start())
            depth, j = 0, i
            while j < len(source):
                if source[j] == "(":
                    depth += 1
                elif source[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            call = source[m.start():j + 1]
            if "encoding=" not in call:
                lineno = source.count("\n", 0, m.start()) + 1
                yield lineno, call.splitlines()[0]


def test_no_text_file_op_without_encoding():
    offenders = []
    for py in PKG.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        for lineno, snippet in _calls_missing_encoding(src):
            offenders.append(f"{py.relative_to(PKG.parent)}:{lineno}: {snippet.strip()}")
    assert not offenders, (
        "text file op without encoding= (breaks on Windows cp1252):\n"
        + "\n".join(offenders)
    )


def test_all_locales_load_as_utf8():
    from email_export_import.gui.i18n import LOCALES_DIR, available_locales
    import json

    for name in available_locales():
        data = json.loads((LOCALES_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert data  # non-empty
