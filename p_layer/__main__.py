"""Allow `python -m p_layer ...` to behave like the `p_layer` CLI."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
