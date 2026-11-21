"""The scope file: the single place that says which technology fields, filing dates and document types you work on.

  <data folder>/scope.json     created by `python pg_cli.py scope init`, edited by you
  schemas/scope.schema.json    the shape; this module adds the checks a schema cannot express

Nothing in the program fixes a CPC code, field name, year or document code. Reading a scope never writes anything."""
import datetime, hashlib, json, shutil
from pathlib import Path
from . import paths
from .validate import errors as schema_errors

SCHEMA_PATH = paths.INSTALL_DIR / 'schemas' / 'scope.schema.json'
EXAMPLE_PATH = paths.INSTALL_DIR / 'examples' / 'scope.example.json'
PRESETS_PATH = Path(__file__).with_name('document_presets.json')
SCOPE_FILE = 'scope.json'


def presets():
    return {k: v for k, v in json.loads(PRESETS_PATH.read_text(encoding='utf-8')).items() if isinstance(v, list)}


def document_codes(scope):
    """The document codes the scope selects: the preset's list, or the custom codes."""
    d = scope.get('documents') or {}
    preset = d.get('preset', 'none')
    return list(d.get('codes') or []) if preset == 'custom' else list(presets()[preset])


def scope_path(data_dir=None):
    return paths.resolve_data_dir(data_dir) / SCOPE_FILE


def _date(s):
    try: return datetime.date.fromisoformat(s)
    except (TypeError, ValueError): return None


def problems(scope):
    """All problems with a scope as a list of plain sentences (empty list = valid)."""
    if not isinstance(scope, dict): return ['scope must be a JSON object']
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    out = list(schema_errors(scope, schema))
    names = []
    for i, f in enumerate(scope.get('fields') if isinstance(scope.get('fields'), list) else []):
        n = f.get('name') if isinstance(f, dict) else None
        if not isinstance(n, str): continue
        try: paths.check_field(n)
        except paths.PathError as e: out.append(f'$.fields[{i}].name: {e}')
        names.append(n)
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup: out.append('$.fields: duplicate field names: ' + ', '.join(dup))
    fl = scope.get('filing') if isinstance(scope.get('filing'), dict) else {}
    d0, d1 = _date(fl.get('from')), _date(fl.get('to_exclusive'))
    if 'from' in fl and d0 is None: out.append('$.filing.from: not a real date')
    if 'to_exclusive' in fl and d1 is None: out.append('$.filing.to_exclusive: not a real date')
    if d0 and d1 and not d0 < d1: out.append('$.filing: from must be earlier than to_exclusive')
    if d0 and d0.year < 1976: out.append('$.filing.from: the USPTO file-wrapper data does not go back before 1976')
    py = scope.get('publication_years')
    if isinstance(py, dict) and 'from' in py and 'to' in py and py['from'] > py['to']: out.append('$.publication_years: from is after to')
    doc = scope.get('documents') if isinstance(scope.get('documents'), dict) else {}
    if doc.get('preset') == 'custom' and not doc.get('codes'): out.append('$.documents: preset custom needs a non-empty codes list')
    if doc.get('preset') in ('none', 'office-actions', 'prosecution', 'procedural') and doc.get('codes'):
        out.append('$.documents: codes are used only with preset custom (remove them or change the preset)')
    return out


def load(data_dir=None, path=None):
    """(scope dict, path). Raises FileNotFoundError when there is no scope, ValueError when it is not valid JSON or not valid."""
    p = paths.checked_path(path, 'the scope path') if path else scope_path(data_dir)
    if not p.is_file(): raise FileNotFoundError(f'no scope file at {p}; create one with: python pg_cli.py scope init')
    raw = p.read_bytes()
    try: scope = json.loads(raw.decode('utf-8-sig'))
    except ValueError as e: raise ValueError(f'{p} is not valid JSON ({e})')
    errs = problems(scope)
    if errs: raise ValueError(f'{p} is not a valid scope:\n  ' + '\n  '.join(errs))
    return scope, p


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def seeds_from_scope(scope):
    """The seeds mapping cpc_expand.py reads: {field: {"seeds": [...], "historical_prefixes": [...]}}."""
    return {f['name']: {'seeds': list(f['seeds']), 'historical_prefixes': list(f.get('historical_prefixes') or [])} for f in scope['fields']}


def years(scope):
    d0, d1 = _date(scope['filing']['from']), _date(scope['filing']['to_exclusive'])
    last = (d1 - datetime.timedelta(days=1)).year
    return d0.year, last


def init(data_dir=None, force=False):
    """Copy the synthetic example to <data folder>/scope.json for you to edit. Never overwrites unless force. Returns the path."""
    d = paths.data_dir_for_write(data_dir)
    p = d / SCOPE_FILE
    if p.exists() and not force: raise FileExistsError(f'{p} already exists; edit it, or pass --force to replace it with the example')
    shutil.copyfile(EXAMPLE_PATH, p)
    return p


def summary(scope):
    y0, y1 = years(scope)
    return {'label': scope.get('label'), 'fields': [f['name'] for f in scope['fields']], 'filing_from': scope['filing']['from'],
            'filing_to_exclusive': scope['filing']['to_exclusive'], 'filing_years': [y0, y1], 'window_days': scope['filing'].get('window_days', 7),
            'documents_preset': (scope.get('documents') or {}).get('preset', 'none'), 'document_codes': document_codes(scope),
            'data_types': scope.get('data_types', []), 'limits': scope.get('limits') or {}}
