"""``python -m palletscan``: how the supervisor, demo and CPU tools spawn
children — immune to PATH/venv-Scripts drift, identical on both platforms."""

import sys

from palletscan.cli import main

if __name__ == "__main__":
    sys.exit(main())
