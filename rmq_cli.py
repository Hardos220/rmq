"""Entry point used by bin/rmq.cmd and bin/rmq (works from any current directory)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rmq.cli import run  # noqa: E402

sys.exit(run())
