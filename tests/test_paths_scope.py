"""pg_tool/paths.py (one data folder, install folder protected, reserved names) and pg_tool/scope.py (scope.json)."""
import copy, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock

from pg_tool import paths, scope as scope_mod

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((ROOT / 'examples' / 'scope.example.json').read_text(encoding='utf-8'))


class DataFolder(unittest.TestCase):
    def test_order_is_argument_then_environment_then_default(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            with mock.patch.dict(os.environ, {paths.ENV_DATA: a}):
                self.assertEqual(paths.resolve_data_dir(b), Path(b).resolve())
                self.assertEqual(paths.resolve_data_dir(), Path(a).resolve())
            env = {k: v for k, v in os.environ.items() if k != paths.ENV_DATA}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(paths.resolve_data_dir(), paths.default_data_dir().resolve())

    def test_default_is_a_per_user_folder_outside_the_install_folder(self):
        d = paths.default_data_dir()
        self.assertFalse(paths.is_inside_install(d))
        self.assertTrue(d.name in ('PatentGenius-Data', 'patent-genius-data'))
        if os.name == 'nt':
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': r'D:\local-app-data'}):
                self.assertEqual(paths.default_data_dir(), Path(r'D:\local-app-data\Brightline\PatentGenius-Data'))

    def test_writing_inside_the_install_folder_is_refused(self):
        for bad in (paths.INSTALL_DIR, paths.INSTALL_DIR / 'corpus', paths.INSTALL_DIR / 'a' / 'b', paths.INSTALL_DIR / 'x' / '..' / 'y'):
            with self.assertRaises(paths.PathError, msg=str(bad)): paths.refuse_install_write(bad)
            with self.assertRaises(paths.PathError): paths.data_dir_for_write(bad)
        self.assertFalse((paths.INSTALL_DIR / 'corpus').exists())
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(paths.data_dir_for_write(Path(t) / 'new'), (Path(t) / 'new').resolve())   # created
            self.assertTrue((Path(t) / 'new').is_dir())

    def test_a_sibling_with_the_same_prefix_is_not_inside(self):
        self.assertFalse(paths.is_inside_install(str(paths.INSTALL_DIR) + '-data'))


class Identifiers(unittest.TestCase):
    def test_field_names(self):
        for ok in ('agri', 'a', 'soil_sensing', 'x1', 'c' + 'o' * 30 + 'n'):
            self.assertEqual(paths.check_field(ok), ok)
        for bad in ('', 'Agri', '1agri', '_x', 'a-b', 'a b', 'a' * 33, 'ünï', 'a.b', None, 5):
            with self.assertRaises(paths.PathError, msg=repr(bad)): paths.check_field(bad)

    def test_reserved_windows_device_names_are_refused_in_fields_and_frame_ids(self):
        reserved = ['con', 'prn', 'aux', 'nul'] + [f'com{i}' for i in range(1, 10)] + [f'lpt{i}' for i in range(1, 10)]
        for name in reserved:
            with self.assertRaises(paths.PathError, msg=name): paths.check_field(name)
            with self.assertRaises(paths.PathError, msg=name): paths.check_frame_id(name)
        for fine in ('conx', 'com10', 'lpt0', 'nulls', 'aux_1', 'console'):
            paths.check_field(fine); paths.check_frame_id(fine)

    def test_frame_ids(self):
        for ok in ('y2010-agri', 'batch-one', 'a', '0frame', 'f_1-2', 'a' * 64):
            self.assertEqual(paths.check_frame_id(ok), ok)
        for bad in ('', 'Y2010', '-x', '_x', 'a b', 'a.b', 'a/b', 'a\\b', '..', 'a' * 65, None):
            with self.assertRaises(paths.PathError, msg=repr(bad)): paths.check_frame_id(bad)


class ScopeFile(unittest.TestCase):
    def sc(self, **changes):
        s = copy.deepcopy(EXAMPLE); s.update(changes); return s

    def test_example_is_valid_and_synthetic(self):
        self.assertEqual(scope_mod.problems(EXAMPLE), [])
        self.assertEqual([f['name'] for f in EXAMPLE['fields']], ['agri', 'soil_sensing'])
        self.assertEqual(scope_mod.years(EXAMPLE), (2012, 2013))   # years before 2015 are the normal case
        self.assertEqual(scope_mod.document_codes(EXAMPLE), [])    # the default document preset is none

    def test_custom_field_names_and_early_years_are_accepted(self):
        s = self.sc(fields=[{'name': 'agri', 'seeds': ['A01B']}], filing={'from': '1999-01-01', 'to_exclusive': '2001-01-01', 'window_days': 14})
        self.assertEqual(scope_mod.problems(s), []); self.assertEqual(scope_mod.years(s), (1999, 2000))

    def test_bad_values_are_reported(self):
        cases = {
            'reserved field': self.sc(fields=[{'name': 'nul', 'seeds': ['A01B']}]),
            'upper case field': self.sc(fields=[{'name': 'Agri', 'seeds': ['A01B']}]),
            'duplicate fields': self.sc(fields=[{'name': 'a', 'seeds': ['A01B']}, {'name': 'a', 'seeds': ['A01C']}]),
            'no fields': self.sc(fields=[]),
            'bad seed': self.sc(fields=[{'name': 'a', 'seeds': ['agri']}]),
            'backwards dates': self.sc(filing={'from': '2013-01-01', 'to_exclusive': '2012-01-01'}),
            'impossible date': self.sc(filing={'from': '2012-02-30', 'to_exclusive': '2013-01-01'}),
            'before 1976': self.sc(filing={'from': '1970-01-01', 'to_exclusive': '1980-01-01'}),
            'window too long': self.sc(filing={'from': '2012-01-01', 'to_exclusive': '2013-01-01', 'window_days': 90}),
            'custom without codes': self.sc(documents={'preset': 'custom', 'codes': []}),
            'codes with preset': self.sc(documents={'preset': 'prosecution', 'codes': ['CTNF']}),
            'unknown preset': self.sc(documents={'preset': 'everything'}),
            'unknown key': self.sc(owner_notes='x'),
            'wrong version': self.sc(scope_version=2),
            'pub years backwards': self.sc(publication_years={'from': 2014, 'to': 2012}),
        }
        for name, s in cases.items():
            self.assertTrue(scope_mod.problems(s), name)
        self.assertTrue(scope_mod.problems([]) and scope_mod.problems('x'))

    def test_document_presets(self):
        p = scope_mod.presets()
        self.assertEqual(p['none'], []); self.assertEqual(p['office-actions'], ['CTNF', 'CTFR'])
        self.assertIn('NOA', p['prosecution']); self.assertIn('N/AP', p['procedural'])
        s = self.sc(documents={'preset': 'custom', 'codes': ['CTNF', 'A.NE']})
        self.assertEqual(scope_mod.document_codes(s), ['CTNF', 'A.NE'])
        self.assertEqual(scope_mod.document_codes(self.sc(documents={'preset': 'prosecution'})), p['prosecution'])

    def test_seeds_from_scope_match_the_seeds_file_format(self):
        seeds = scope_mod.seeds_from_scope(EXAMPLE)
        example_seeds = json.loads((ROOT / 'examples' / 'seeds.example.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(seeds), sorted(example_seeds))
        self.assertEqual(seeds['soil_sensing']['historical_prefixes'], ['G01N33/'])

    def test_init_load_and_refusals(self):
        with tempfile.TemporaryDirectory() as t:
            p = scope_mod.init(t)
            self.assertEqual(p, Path(t).resolve() / 'scope.json')
            sc, path = scope_mod.load(t); self.assertEqual(sc['fields'][0]['name'], 'agri')
            with self.assertRaises(FileExistsError): scope_mod.init(t)
            scope_mod.init(t, force=True)
            p.write_text('{ not json', encoding='utf-8')
            with self.assertRaises(ValueError): scope_mod.load(t)
            p.write_text(json.dumps(self.sc(fields=[])), encoding='utf-8')
            with self.assertRaises(ValueError) as cm: scope_mod.load(t)
            self.assertIn('fields', str(cm.exception))
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(FileNotFoundError): scope_mod.load(t)
        with self.assertRaises(paths.PathError): scope_mod.init(paths.INSTALL_DIR / 'scratch')
        self.assertFalse((paths.INSTALL_DIR / 'scratch').exists())

    def test_utf8_bom_is_tolerated(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / 'scope.json').write_bytes(b'\xef\xbb\xbf' + json.dumps(EXAMPLE).encode('utf-8'))
            scope_mod.load(t)

    def test_shipped_scope_material_is_synthetic(self):
        for field in EXAMPLE['fields']:
            for s in field['seeds']: self.assertTrue(s.startswith(('A01', 'G01N')), s)   # the invented fields use agriculture and soil-testing codes only


if __name__ == '__main__':
    unittest.main()
