"""Structural document shells: templates/<name>.md (section headings with bracketed prompts) and templates/<name>.json
(review flags). Sections are parsed from the Markdown file and pinned to its sha256; a parse failure yields status partial.
Every shell is reachable by its key. Only these shells exist: this program loads no other templates, from any folder."""
import json, re
from .local_data import ROOT, sha256_file

SHELLS = ('office-action-response', 'restriction-election', 'oa-issue-inventory', 'interview-notes', 'client-update', 'invention-intake', 'application', 'summary')
HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*$', re.M)


def sections(md):
    out = []
    hs = list(HEADING.finditer(md))
    for i, m in enumerate(hs):
        title = m.group(2)
        num = re.match(r'(\d+(?:\.\d+)*)\.?\s', title)
        out.append({'number': num.group(1) if num else None, 'heading': title, 'level': len(m.group(1)),
                    'offsets': [m.start(), hs[i + 1].start() if i + 1 < len(hs) else len(md)]})
    return out


def get(key, include_markdown=True, want_sections=None):
    """-> (status, data, warnings)."""
    if key not in SHELLS: return 'not_found', None, ['unknown template key']
    path = ROOT / 'templates' / f'{key}.md'
    if not path.exists(): return 'not_found', None, [f'template file missing: templates/{path.name}']
    md = path.read_text(encoding='utf-8')
    secs = sections(md)
    title = HEADING.search(md).group(2) if HEADING.search(md) else key
    caveats = []
    shell = ROOT / 'templates' / f'{key}.json'
    if shell.exists():
        sj = json.loads(shell.read_text(encoding='utf-8')); caveats = [f'Review: {x}' for x in sj.get('review_flags', [])]
    if want_sections:
        keep = lambda n: n and any(n == w or n.startswith(w + '.') for w in want_sections)
        sel = [s for s in secs if keep(s['number'])]
    else: sel = secs
    data = {'key': key, 'template_id': key, 'path': f'templates/{path.name}', 'sha256': sha256_file(path), 'title': title, 'kind': 'structural-shell', 'anchor': None,
            'sections': sel, 'evidence_tables': [], 'caveats': caveats, 'evidence_sources': [], 'applies_to': None, 'version': None, 'template_status': None}
    if include_markdown:
        data['markdown'] = ''.join(md[s['offsets'][0]:s['offsets'][1]] for s in sel) if want_sections else md
    return 'ok', data, []
