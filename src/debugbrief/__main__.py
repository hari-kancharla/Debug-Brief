"""Allow running the CLI via ``python -m debugbrief``."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
