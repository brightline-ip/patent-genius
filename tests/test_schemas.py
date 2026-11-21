"""Schemas in schemas/. Structural checks use the standard library; full
JSON Schema validation runs only when the optional jsonschema package is installed."""
import copy, json, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {name: json.loads((ROOT / 'schemas' / f'{name}.schema.json').read_text(encoding='utf-8'))
           for name in ('office-action-analysis', 'response-plan', 'scope')}
try:
    from jsonschema import Draft202012Validator
except ImportError:  # optional dependency
    Draft202012Validator = None


def load(p): return json.loads(Path(p).read_text(encoding='utf-8'))


@unittest.skipIf(Draft202012Validator is None, 'jsonschema not installed')
class SchemaValidation(unittest.TestCase):
    def errors(self, schema, inst):
        v = Draft202012Validator(SCHEMAS[schema], format_checker=Draft202012Validator.FORMAT_CHECKER)
        return [e.message for e in v.iter_errors(inst)]

    def test_schemas_are_valid(self):
        for s in SCHEMAS.values(): Draft202012Validator.check_schema(s)

    def test_examples_validate(self):
        self.assertEqual(self.errors('office-action-analysis', load(ROOT / 'examples/oa-analysis.example.json')), [])
        self.assertEqual(self.errors('response-plan', load(ROOT / 'examples/response-plan.example.json')), [])
        self.assertEqual(self.errors('scope', load(ROOT / 'examples/scope.example.json')), [])

    def test_new_claim_needs_support(self):
        plan = load(ROOT / 'examples/response-plan.example.json')
        bad = copy.deepcopy(plan); bad['amendments'][1]['support'] = []
        self.assertTrue(self.errors('response-plan', bad))

    def test_evidence_number_needs_its_table(self):
        plan = load(ROOT / 'examples/response-plan.example.json')
        bad = copy.deepcopy(plan); del bad['route']['evidence'][0]['table']
        self.assertTrue(self.errors('response-plan', bad))

    def test_double_patenting_needs_conflicting_patent(self):
        a = load(ROOT / 'examples/oa-analysis.example.json')
        bad = copy.deepcopy(a); del bad['rejections'][1]['conflicting_application_or_patent']
        self.assertTrue(self.errors('office-action-analysis', bad))

    def test_vocabulary_is_statute_specific(self):
        a = load(ROOT / 'examples/oa-analysis.example.json')
        bad = copy.deepcopy(a); bad['rejections'][0]['categories'] = ['mental_process']  # a 101 category on a 103 ground
        self.assertTrue(self.errors('office-action-analysis', bad))


if __name__ == '__main__':
    unittest.main()
