"""TEST ONLY. Runs pg_cli.py with the smallest-group floor lowered to 1, so that tiny synthetic data can be used end to end in a subprocess.
The shipped program has no switch for this: the floor is a constant in pg_tool/stats.py and the minimum of min_n is in the contract; this launcher
changes both, in its own process only (see lowfloor.py)."""
import os, runpy, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# pg_cli.py reads --data-dir after importing the service; importing the service here first needs the folder already in the environment
if '--data-dir' in sys.argv: os.environ['PG_TOOL_DATA_DIR'] = str(Path(sys.argv[sys.argv.index('--data-dir') + 1]).expanduser().resolve())
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lowfloor  # noqa: E402

lowfloor.patch_floor()
sys.argv[0] = str(ROOT / 'pg_cli.py')
runpy.run_path(str(ROOT / 'pg_cli.py'), run_name='__main__')
