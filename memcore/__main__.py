"""Allow `python -m memcore ...` to behave like the `memcore` CLI."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
