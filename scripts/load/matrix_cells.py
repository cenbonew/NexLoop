"""NX-052 full-window states (implementation note §11): run_on_lane.sh's matrix runner restricted to $NEXLOOP_LOAD_CELLS.

    LOAD_RUNNER_CMD=scripts/load/matrix_cells.py NEXLOOP_LOAD_CELLS=C4,C4-peak scripts/load/run_on_lane.sh

The lane script calls the runner with the usual matrix.py arguments; this adds --cells and runs matrix.py unchanged.
"""
import os
import runpy
import sys
from pathlib import Path

cells = os.environ.get('NEXLOOP_LOAD_CELLS', '')
if not cells:
    raise SystemExit('NEXLOOP_LOAD_CELLS required, for example C4,C4-peak')
sys.argv = [str(Path(__file__).with_name('matrix.py')), *sys.argv[1:], '--cells', cells]
sys.path.insert(0, str(Path(__file__).parent))
runpy.run_path(sys.argv[0], run_name='__main__')
