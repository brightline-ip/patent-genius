"""One place for the rule that small groups are never shown as rates: the smallest group size (min_n) any tool will use.

A caller may ask for a larger minimum. A smaller one is refused by the contract (MCP and the command line answer invalid_input); code that calls the
library directly and bypasses the contract gets MIN_N_FLOOR and a warning. The finder, the pack and
pg_trends all use this. There is no environment variable, option or file that lowers the floor: the shipped program has no switch for it.
The tests lower it by patching MIN_N_FLOOR in their own process (tests/pg_cli_lowfloor.py does that for a subprocess)."""

MIN_N_FLOOR = 5


def floor():
    return MIN_N_FLOOR


def clamp_min_n(requested, default):
    """(min_n to use, warning or None). `requested` None means the caller's default."""
    want = default if requested is None else int(requested)
    fl = floor()
    use = max(fl, want)
    return use, (f'min_n {want} is below the floor of {fl}: {use} was used' if use != want and requested is not None else None)
