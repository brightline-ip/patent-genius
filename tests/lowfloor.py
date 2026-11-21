"""TEST ONLY. Tiny synthetic data needs a smallest-group size below the floor of 5. patch_floor() lowers the floor constant in pg_tool/stats.py and the
`minimum` of every min_n in the loaded contract, both in this process only, and returns a function that undoes it. The shipped program has no switch for this."""
import copy
from unittest import mock

from pg_tool import service, stats


def _relax(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == 'min_n' and isinstance(v, dict) and v.get('minimum') == stats.MIN_N_FLOOR: v['minimum'] = 1
            else: _relax(v)
    elif isinstance(node, list):
        for v in node: _relax(v)


def patch_floor():
    relaxed = copy.deepcopy(service.CONTRACT); _relax(relaxed)
    ps = [mock.patch.object(stats, 'MIN_N_FLOOR', 1), mock.patch.object(service, 'CONTRACT', relaxed)]
    for p in ps: p.start()
    return lambda: [p.stop() for p in reversed(ps)]
