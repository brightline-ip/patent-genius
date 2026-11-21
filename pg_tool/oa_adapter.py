"""Adapter between Patent Genius and the service that answers office-action rejection questions.

Everything that knows the shape of that service lives here: the request body sent to the Hub's `oa-rejections` route, how a
response page is read, and how one provider row becomes a canonical row for the database. Today there is one adapter, for the
USPTO "Office Action Rejection API v2" (the portal marks it "Legacy"). If the USPTO moves the data to its Office Action Search
API, a second class here and one line in ADAPTERS are the whole change: the collector, the loader and the finder only use the
methods below.

What the live service was seen to return (a small read-only probe, 2026-11-20; see docs/COLLECTING.md):
  legalSectionCode    "102", "103", "112", "Other" or empty. It names the STATUTE only: there are no 112(a)/(b) subsections here.
  actionTypeCategory  rejected, objected, allowed, cancelled or empty. A paragraph row is a rejection only when it says rejected.
  hasRej101 hasRej102 hasRej103 hasRej112 hasRejDP   provider flags, kept as they are (they also fire on quoted statute text)
  There is no examiner name and no cited-reference identity in these rows, only counts of citations."""
import json, re

STATUTES = ('101', '102', '103', '112')
_STATUTE = re.compile(r'^\s*(?:35\s*(?:U\.?\s?S\.?\s?C\.?)?\s*§?\s*)?(101|102|103|112)\b', re.I)


class AdapterError(Exception):
    """The response did not have the shape the adapter documents. Never read as 'no records'."""


def _ci(doc):
    return {str(k).lower(): v for k, v in doc.items()}


def _first(low, *names):
    for n in names:
        if low.get(n) is not None and low.get(n) != '': return low[n]
    return None


def _int(v):
    try: return int(v) if v is not None and str(v) != '' else None
    except (TypeError, ValueError): return None


def _flag(v):
    return int(str(v).strip().lower() in ('1', 'true', 'y', 'yes')) if v is not None else 0


def statute_of(legal_section_code):
    """Statute level only: '101' | '102' | '103' | '112' | 'dp' | 'other' | None (empty). A string such as '112(b)' maps to '112'."""
    s = str(legal_section_code or '').strip()
    if not s: return None
    if 'double patenting' in s.lower(): return 'dp'
    m = _STATUTE.match(s)
    return m.group(1) if m else 'other'


def row_kind(action_type):
    t = str(action_type or '').strip().lower()
    return {'rejected': 'rejection', 'objected': 'objection', 'allowed': 'allowed', 'cancelled': 'cancelled', '': 'unlabelled'}.get(t, 'other')


class LegacyRejectionsV2:
    id = 'legacy-rejections-v2'
    route = 'oa-rejections'
    description = 'USPTO Office Action Rejection API v2 (Open Data Portal, marked Legacy), reached through the Hub route oa-rejections'
    max_applications_per_request = 50   # the Hub accepts up to 100; 50 keeps a page set small
    page_rows = 100                     # the Hub accepts 1 to 100
    expected_fields = ('hasRej101', 'hasRej102', 'hasRej103', 'hasRej112', 'hasRejDP', 'legalSectionCode', 'actionTypeCategory',
                       'paragraphNumber', 'groupArtUnitNumber', 'submissionDate', 'cite103Max')

    def request(self, applications, start=0, rows=None):
        return {'action': 'records', 'application_numbers': list(applications), 'start': int(start), 'rows': int(rows or self.page_rows)}

    def fields_request(self):
        return {'action': 'fields'}

    def parse_page(self, envelope):
        """(num_found, docs) from a 200 envelope; AdapterError for anything that is not the documented shape."""
        try: body = json.loads(envelope.get('text') or '')
        except (ValueError, TypeError): raise AdapterError('the response is not JSON')
        r = body.get('response') if isinstance(body, dict) else None
        if not isinstance(r, dict) or not isinstance(r.get('numFound'), int) or not isinstance(r.get('docs'), list) or r['numFound'] < 0:
            raise AdapterError('the response has no response.numFound and response.docs')
        return r['numFound'], r['docs']

    def parse_fields(self, envelope):
        try: body = json.loads(envelope.get('text') or '')
        except (ValueError, TypeError): raise AdapterError('the field list is not JSON')
        f = body.get('fields') if isinstance(body, dict) else None
        if not isinstance(f, list): raise AdapterError('the field list is missing')
        names = []
        for x in f:
            n = x if isinstance(x, str) else (x.get('name') or x.get('field') or x.get('fieldName')) if isinstance(x, dict) else None
            if isinstance(n, str): names.append(n)
        return names

    def normalise(self, doc):
        """One provider row -> canonical row (the database columns). Unknown fields are ignored, missing ones are None."""
        low = _ci(doc)
        sec = _first(low, 'legalsectioncode'); act = _first(low, 'actiontypecategory')
        claims = low.get('claimnumberarraydocument')
        return {'application': _first(low, 'patentapplicationnumber', 'applicationnumbertext'),
                'oa_document': _first(low, 'obsoletedocumentidentifier'), 'doc_code': _first(low, 'legacydocumentcodeidentifier'),
                'date': (str(_first(low, 'submissiondate') or '')[:10] or None), 'legal_section': sec, 'statute': statute_of(sec),
                'action_type': act, 'row_kind': row_kind(act),
                'claims': ','.join(str(c) for c in claims) if isinstance(claims, list) and claims else None,
                'art_unit': _first(low, 'groupartunitnumber'), 'paragraph_number': _int(_first(low, 'paragraphnumber')),
                'alice': _flag(low.get('aliceindicator')), 'mayo': _flag(low.get('mayoindicator')), 'bilski': _flag(low.get('bilskiindicator')),
                'myriad': _flag(low.get('myriadindicator')), 'allowed_claim': _flag(low.get('allowedclaimindicator')),
                'cite102_gt1': _int(_first(low, 'cite102gt1')), 'cite103_gt3': _int(_first(low, 'cite103gt3')),
                'cite103_eq1': _int(_first(low, 'cite103eq1', 'cite103eqone')), 'cite103_max': _int(_first(low, 'cite103max')),
                'has_rej101': _flag(low.get('hasrej101')), 'has_rej102': _flag(low.get('hasrej102')), 'has_rej103': _flag(low.get('hasrej103')),
                'has_rej112': _flag(low.get('hasrej112')), 'has_rejdp': _flag(low.get('hasrejdp'))}


ADAPTERS = {LegacyRejectionsV2.id: LegacyRejectionsV2}
DEFAULT_ADAPTER = LegacyRejectionsV2.id


def get(adapter_id=None):
    cls = ADAPTERS.get(adapter_id or DEFAULT_ADAPTER)
    if cls is None: raise AdapterError(f'unknown adapter {str(adapter_id)[:40]!r}; this release has: ' + ', '.join(ADAPTERS))
    return cls()
