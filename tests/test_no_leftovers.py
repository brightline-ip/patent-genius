"""Things that were deliberately removed must stay removed: the adaptive window example, any hook that loads drafting
templates from another folder, and fixed technology, year or document-code values in the program."""
import json, re, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).name
CODE = [p for p in ROOT.rglob('*') if p.suffix in ('.py', '.mjs') and 'tests' not in p.parts and '__pycache__' not in p.parts and '.git' not in p.parts]


class Removed(unittest.TestCase):
    def test_adaptive_policy_is_gone(self):
        self.assertFalse((ROOT / ('adaptive_' + 'frame_plan.py')).exists())
        hits = [p.name for p in CODE if re.search(r'adaptive', p.read_text(encoding='utf-8'), re.I)]
        self.assertEqual(hits, [])

    def test_no_template_loading_hook(self):
        hook = re.compile(r'PRIVATE_TEMPLATES|templates[\/]+meta|_ALL_KEYS|evidence_template')
        self.assertEqual([p.name for p in CODE if hook.search(p.read_text(encoding='utf-8'))], [])
        self.assertFalse((ROOT / 'templates' / 'meta').exists()); self.assertFalse((ROOT / 'schemas' / 'template-metadata.schema.json').exists())
        shells = sorted(p.stem for p in (ROOT / 'templates').glob('*.md'))
        from pg_tool import templates
        self.assertEqual(shells, sorted(templates.SHELLS))                  # every shipped shell is reachable, and nothing else is looked up

    def test_the_contract_fixes_no_field_or_year(self):
        c = json.loads((ROOT / 'pg_tool' / 'contract.json').read_text(encoding='utf-8'))
        text = json.dumps(c)
        self.assertNotIn('"minimum": 2015', text)
        for name in ('SearchInput', 'TrendsInput', 'AssessInput', 'ReportInput'):
            self.assertNotIn('enum', json.dumps(self.stratum(c['$defs'][name])), name)

    def stratum(self, node):
        found = []
        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == 'stratum': found.append(v)
                    else: walk(v)
            elif isinstance(o, list):
                for v in o: walk(v)
        walk(node); return found

    def test_no_fixed_analysis_window_in_the_database_reports(self):
        import pg_db
        for name, (_, sql) in pg_db.REPORTS.items():
            if sql: self.assertNotRegex(sql, r'BETWEEN\s+\d{4}\s+AND\s+\d{4}', name)

    def test_no_residual_counts_or_pointers_to_unshipped_docs(self):
        for p in ROOT.rglob('*'):
            if p.suffix in ('.py', '.mjs', '.md', '.json') and p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts and p.name != SELF and p.name != 'test_release_allowlist.py':
                t = p.read_text(encoding='utf-8', errors='replace')
                self.assertNotRegex(t, r'\b\d{2,3},\d{3} of \d{2,3},\d{3} records\b', p.name)
                self.assertNotIn('ROADMAP', t, p.name)


if __name__ == '__main__':
    unittest.main()
