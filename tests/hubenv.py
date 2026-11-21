"""Test support: a scripted stand-in for the Hub gateway (tests/gateway_stub.py) and an environment that can never reach a real Hub,
a real key or the real per-user folders."""
import json, os, sys
from pathlib import Path
from unittest import mock

import atexit, shutil, tempfile
HERE = Path(__file__).resolve().parent
# The Hub keeps limiter state per user; tests point it at a throwaway folder so they can never touch the real profile.
LIMITER_DIR = tempfile.mkdtemp(prefix='pg-limiter-'); atexit.register(shutil.rmtree, LIMITER_DIR, True)
STUB = HERE / 'gateway_stub.py'
KEYISH = ('API_KEY', '_TOKEN', '_SECRET', 'CONSUMER_KEY', '_PASSWORD')


def clean_environ(base=None, **extra):
    """A copy of the environment with no provider key, no Hub setting and no Hub look-up in the default folder."""
    env = {k: v for k, v in (base if base is not None else os.environ).items()
           if not k.upper().endswith(KEYISH) and not k.startswith(('PG_HUB', 'PG_TOOL', 'PG_ALLOW', 'PG_STUB', 'FRAME_', 'PG_BUDGET'))}
    env['PG_HUB_DISCOVER'] = '0'; env['PATENT_RESEARCH_LIMITER_DIR'] = LIMITER_DIR
    env.update({k: str(v) for k, v in extra.items()})
    return env


class StubHub:
    """Writes the stub's script file in `folder`; .env() gives the environment that makes Patent Genius use it."""
    def __init__(self, folder, rows=None, script=None, **cfg):
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        self.path, self.state, self.log = folder / 'stub-script.json', folder / 'stub-state.json', folder / 'stub-log.jsonl'
        self.cfg = {'state': str(self.state), 'log': str(self.log), 'rows': rows or {}, 'script': script or {}, **cfg}
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.cfg), encoding='utf-8')

    def env(self, **extra):
        return clean_environ(PG_HUB_TEST_STUB=json.dumps([sys.executable, str(STUB)]), PG_ALLOW_TEST_STUB='1', PG_STUB_SCRIPT=str(self.path), **extra)

    def patch(self, **extra):
        """mock.patch.dict that replaces the whole environment (so keys and Hub settings of the machine running the tests are gone)."""
        return mock.patch.dict(os.environ, self.env(**extra), clear=True)

    def calls(self):
        return [json.loads(l) for l in self.log.read_text(encoding='utf-8').splitlines()] if self.log.exists() else []

    def call_count(self):
        try: return json.loads(self.state.read_text(encoding='utf-8'))['calls']
        except (OSError, ValueError): return 0


def provider_row(app, doc, code, date, statute='103', action='rejected', paragraph=1, art_unit='1234', **flags):
    """One provider row in the shape of the Office Action Rejection API (field names as seen in the field list)."""
    row = {'patentApplicationNumber': app, 'obsoleteDocumentIdentifier': doc, 'legacyDocumentCodeIdentifier': code, 'submissionDate': date + 'T00:00:00',
           'legalSectionCode': statute, 'actionTypeCategory': action, 'claimNumberArrayDocument': ['1', '2'], 'groupArtUnitNumber': art_unit,
           'paragraphNumber': paragraph, 'aliceIndicator': 0, 'mayoIndicator': 0, 'cite103Max': 2}
    for s in ('101', '102', '103', '112'): row['hasRej' + s] = 1 if (action == 'rejected' and statute == s) else 0
    row['hasRejDP'] = 0
    row.update(flags)
    return row
