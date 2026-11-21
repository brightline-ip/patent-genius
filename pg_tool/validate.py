"""Standard-library JSON Schema (draft 2020-12) validator for the subset the contract and the schemas/ files use:
type, enum, const, pattern, minLength/maxLength, minimum/maximum, required, properties, additionalProperties,
items, prefixItems, minItems/maxItems, uniqueItems, minProperties, oneOf/anyOf/allOf, if/then, $ref (local).
Errors name the JSON path and the rule, never the offending value, so caller text is not echoed."""
import re

TYPES = {'object': dict, 'array': list, 'string': str, 'boolean': bool, 'null': type(None)}


def _is(v, t):
    if t == 'integer': return isinstance(v, int) and not isinstance(v, bool)
    if t == 'number': return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, TYPES[t])


def _ref(root, ref):
    if not ref.startswith('#/'): raise ValueError('only local $ref supported')
    node = root
    for part in ref[2:].split('/'): node = node[part.replace('~1', '/').replace('~0', '~')]
    return node


def errors(value, schema, root=None, path='$'):
    root = root if root is not None else schema
    out = []
    if schema is True or schema == {}: return out
    if schema is False: return [f'{path}: not allowed']
    if '$ref' in schema:
        out += errors(value, _ref(root, schema['$ref']), root, path)
    t = schema.get('type')
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is(value, x) for x in ts): return out + [f'{path}: expected {"/".join(ts)}']
    if 'const' in schema and value != schema['const']: out.append(f'{path}: must equal the constant')
    if 'enum' in schema and value not in schema['enum']: out.append(f'{path}: not one of the allowed values')
    if isinstance(value, str):
        if 'maxLength' in schema and len(value) > schema['maxLength']: out.append(f'{path}: longer than {schema["maxLength"]}')
        if 'minLength' in schema and len(value) < schema['minLength']: out.append(f'{path}: shorter than {schema["minLength"]}')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value): out.append(f'{path}: does not match the required pattern')
    if _is(value, 'number'):
        if 'minimum' in schema and value < schema['minimum']: out.append(f'{path}: below {schema["minimum"]}')
        if 'maximum' in schema and value > schema['maximum']: out.append(f'{path}: above {schema["maximum"]}')
    if isinstance(value, dict):
        for k in schema.get('required', []):
            if k not in value: out.append(f'{path}: missing required field {k}')
        props = schema.get('properties', {})
        for k, v in value.items():
            if k in props: out += errors(v, props[k], root, f'{path}.{k}')
            elif schema.get('additionalProperties') is False: out.append(f'{path}: unexpected field {k[:40]}')
            elif isinstance(schema.get('additionalProperties'), dict): out += errors(v, schema['additionalProperties'], root, f'{path}.{k}')
        if 'minProperties' in schema and len(value) < schema['minProperties']: out.append(f'{path}: needs at least {schema["minProperties"]} field(s)')
    if isinstance(value, list):
        pre = schema.get('prefixItems', [])
        for i, v in enumerate(value):
            if i < len(pre): out += errors(v, pre[i], root, f'{path}[{i}]')
            elif 'items' in schema: out += errors(v, schema['items'], root, f'{path}[{i}]')
        if 'minItems' in schema and len(value) < schema['minItems']: out.append(f'{path}: fewer than {schema["minItems"]} items')
        if 'maxItems' in schema and len(value) > schema['maxItems']: out.append(f'{path}: more than {schema["maxItems"]} items')
        if schema.get('uniqueItems') and len({repr(v) for v in value}) != len(value): out.append(f'{path}: items must be unique')
    for sub in schema.get('allOf', []): out += errors(value, sub, root, path)
    if 'anyOf' in schema and not any(not errors(value, s, root, path) for s in schema['anyOf']): out.append(f'{path}: matches none of the allowed forms')
    if 'oneOf' in schema:
        n = sum(1 for s in schema['oneOf'] if not errors(value, s, root, path))
        if n != 1: out.append(f'{path}: must match exactly one allowed form (matched {n})')
    if 'if' in schema and not errors(value, schema['if'], root, path):
        if 'then' in schema: out += errors(value, schema['then'], root, path)
    elif 'if' in schema and 'else' in schema: out += errors(value, schema['else'], root, path)
    return out
