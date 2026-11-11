"""Entry point, run as `python -I broker.py`: isolated mode keeps the current
directory (the project, which Claude can write) and PYTHONPATH off sys.path."""
import os
import sys

PREFIX = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, PREFIX)

from claude_slurm.broker import main  # noqa: E402

main()
