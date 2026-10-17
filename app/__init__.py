"""ASCEnD DocGen. Settings come from environment variables; a .env file in the project root
(KEY=value lines, never committed) is read first so a local setup needs no manual exports."""
import os
from pathlib import Path

_env = Path(__file__).resolve().parents[1] / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _key, _sep, _value = _line.strip().partition("=")
        if _sep and _key and not _key.startswith("#"):
            os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))
