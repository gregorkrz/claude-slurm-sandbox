"""Entry point for a service unit, run as `python -I _service.py POLICY NAME SCRIPT [ARGS...]`
by systemd-run. See claude_slurm/servicehost.py."""
import os
import sys

PREFIX = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, PREFIX)

from claude_slurm.servicehost import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
