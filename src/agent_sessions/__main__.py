"""Module entry point for `python -m agent_sessions`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
