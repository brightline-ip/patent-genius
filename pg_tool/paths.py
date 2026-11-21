"""Where Patent Genius keeps your data, and the one rule that protects the install folder.

There is ONE data folder. It holds scope.json, inputs/, corpus/, public-originals/, packs/, locks/ and the database.
It is chosen in this order:
  1. an explicit argument (the command-line option --data-dir),
  2. the environment variable PG_TOOL_DATA_DIR,
  3. the per-user default: %LOCALAPPDATA%\\Brightline\\PatentGenius-Data on Windows, otherwise
     $XDG_DATA_HOME/brightline/patent-genius-data (or ~/.local/share/brightline/patent-genius-data).

Reading from a data folder is always allowed. Writing is refused when the target is inside the install folder (the
folder that holds this code), so a mistaken setting can never mix your data with the program files or end up in a
source checkout. The same rule is mirrored in hub_config.mjs for the Node scripts.

This module also holds the identifier rules shared by scope files, frame plans and file names."""
import os, re
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parents[1]
ENV_DATA = 'PG_TOOL_DATA_DIR'

FIELD_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
FRAME_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
# Windows treats these names as devices whatever the extension ("con.json.gz" is the console), so they are refused everywhere.
RESERVED_NAMES = frozenset({'con', 'prn', 'aux', 'nul'} | {f'com{i}' for i in range(1, 10)} | {f'lpt{i}' for i in range(1, 10)})


class PathError(ValueError):
    """A path or identifier that Patent Genius refuses to use."""


_DRIVE = re.compile(r'^(?:\\\\[?.]\\)?[A-Za-z]:')


def has_stream_syntax(raw):
    """True when the text has a ':' anywhere except in a drive letter ("C:" or "\\\\?\\C:"). On NTFS "file.txt:name" and "file.txt:name:$DATA" name an
    alternate data stream of file.txt, which is not a file anyone sees in a folder listing; no path with a colon in it is read or written."""
    return ':' in _DRIVE.sub('', str(raw), count=1)


def refuse_stream_syntax(raw, what='path'):
    """Raise PathError for NTFS alternate-data-stream syntax (or any ':' beyond the drive letter). Returns raw."""
    if has_stream_syntax(raw): raise PathError(f'{what} contains a ":" beyond the drive letter (NTFS alternate data streams are never read or written): {str(raw)[:80]!r}')
    return raw


def checked_path(raw, what='path'):
    """Path(raw) after refusing alternate-data-stream syntax. Use for every path a user supplies."""
    return Path(refuse_stream_syntax(raw, what))


def checked_or_exit(raw, what='path'):
    """checked_path for command-line scripts: a refusal ends the program with one line."""
    try: return checked_path(raw, what)
    except PathError as e: raise SystemExit(f'error: {e}')


def _reserved(name):
    return name.split('.', 1)[0].lower() in RESERVED_NAMES


def check_field(name):
    """Validate a field name (a user-defined technology field, for example 'agri'). Returns the name."""
    if not isinstance(name, str) or not FIELD_RE.match(name):
        raise PathError('field names are lower-case letters, digits and underscores, start with a letter, and are at most 32 characters')
    if _reserved(name): raise PathError(f'field name {name!r} is a reserved Windows device name')
    return name


def check_frame_id(frame_id):
    """Validate a frame id (used as a file name). Returns the id."""
    if not isinstance(frame_id, str) or not FRAME_ID_RE.match(frame_id):
        raise PathError('frame ids are lower-case letters, digits, "_" and "-", start with a letter or digit, and are at most 64 characters')
    if _reserved(frame_id): raise PathError(f'frame id {frame_id!r} is a reserved Windows device name')
    return frame_id


def default_data_dir():
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
        return Path(base) / 'Brightline' / 'PatentGenius-Data'
    base = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')
    return Path(base) / 'brightline' / 'patent-genius-data'


def resolve_data_dir(explicit=None):
    """The data folder (not created, not checked for writing). Order: explicit argument, PG_TOOL_DATA_DIR, per-user default."""
    raw = explicit or os.environ.get(ENV_DATA) or default_data_dir()
    refuse_stream_syntax(raw, 'the data folder')
    return Path(raw).expanduser().resolve()


def is_inside_install(path):
    p = Path(path).expanduser().resolve()
    return p == INSTALL_DIR or INSTALL_DIR in p.parents


def refuse_install_write(path):
    """Raise PathError when path is inside the install folder or uses alternate-data-stream syntax. Call before creating or writing anything."""
    refuse_stream_syntax(path, 'the output path')
    if is_inside_install(path):
        raise PathError(f'refusing to write inside the install folder ({INSTALL_DIR}); set PG_TOOL_DATA_DIR or pass --data-dir to a folder outside it')
    return Path(path)


def data_dir_for_write(explicit=None, create=True):
    """The data folder, checked to be outside the install folder and created when missing."""
    d = resolve_data_dir(explicit)
    refuse_install_write(d)
    if create: d.mkdir(parents=True, exist_ok=True)
    return d


def data_path(*parts, data_dir=None):
    return resolve_data_dir(data_dir).joinpath(*parts)
