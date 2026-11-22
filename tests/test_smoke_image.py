"""The image smoke script works against the installed package.

It gates the Docker push at release time, so a script broken by a change on
main would only show then; running it here shows it on every push.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_smoke_script_passes_on_this_install() -> None:
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "smoke_image.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "smoke ok" in out.stdout
