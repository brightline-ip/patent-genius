"""What happened after an office action, derived from file-wrapper events only (no model, no reading of any argument).

For each office action (an application, a date and a document code such as CTNF or CTFR) the action is first ANCHORED. In real collected data the
date of a rejection record is the date of the MAILING event (MCTNF, MCTFR, MCTRS); the action event itself (CTNF, CTFR, CTRS) is usually 1 to 9 or
more days earlier. So the anchor is the mailing event at the record date (mail_exact, or mail_near within 1 day) and, when there is none, the action
event within 14 days before to 1 day after that date (ct_near). With neither the action is unanchored and nothing is claimed about what followed. The
paired action event and the mailing event are the action's own and never count as what came next.
The provider's array order is NOT chronological and is never used. Events are sorted by date and events on the same date are simultaneous. Then the events
strictly AFTER the anchor date are examined:
  next_event / next_event_date   the first date with a turning point (a later Office action, allowance, abandonment, RCE or appeal). When several turning
                                 events share that date the fixed role precedence in event_codes.json (turning_precedence) picks one: RCE, appeal,
                                 allowance, abandonment for a continuation, abandonment, then Office actions. A rescinded abandonment is skipped: the
                                 search goes on to the next turning point after the rescission (abandonment_rescinded = 1)
  interview_before_next          1 when an interview event falls between the action and that turning point, INCLUDING the turning point's own date
  amendment_before_next          1 when a reply/amendment event falls between them (same inclusive rule)
  rce_or_appeal_next             1 when the turning point itself is an RCE or an appeal step, 0 when it is anything else, NULL when none
  next_event_state               recognised | later_events_none_recognised | no_later_event | unanchored
                                 (an open case with later events that match no known code is not the same as one with no later event)
  later_events                   how many events follow the anchor date
  application_outcome            the application's state at the frame's freeze date (granted, abandoned, allowed_not_yet_granted, pending, unknown)

These describe what the applicant and the Office did next. They say nothing about what an argument said and show no cause.
The code lists are DATA (pg_tool/event_codes.json). Every code in it was checked against the official USPTO PatEx 2022 event-code list
(pg_tool/data/uspto_event_codes.csv); the descriptions are the USPTO's, but which codes count as an interview, an abandonment, an amendment and so on
(the roles) is this tool's own mapping. Abandonment codes follow the official list; ABN9 (disposal for an RCE) is NOT an abandonment, AFWC is kept as
"abandoned for continuation", rescinded-abandonment codes make an abandonment non-final, and pre-exam abandonment codes are outside prosecution after an
action. An event whose code has no role is simply not a turning point and is reported in the coverage report with its official description."""
import csv, datetime, json
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name('event_codes.json')
CONFIG = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
CODES = CONFIG['codes']
STATUS_NOTE = CONFIG['status']
ANCHOR = CONFIG['anchor']
MAIL_WINDOW = int(ANCHOR['mail_window_days'])
CT_BEFORE = int(ANCHOR['ct_window_before_days'])
CT_AFTER = int(ANCHOR['ct_window_after_days'])
ANCHORED = ('mail_exact', 'mail_near', 'ct_near')
PRECEDENCE = tuple(CONFIG['turning_precedence'])
MAIL_PAIRS = {k: tuple(v) for k, v in CONFIG['mail_pairs'].items()}     # {action code: (action code, its mailing code)}
MAIL_OF = {v[1]: v[0] for v in MAIL_PAIRS.values()}                     # {mailing code: action code}
TURNING_ROLES = frozenset(CONFIG['turning_roles'])
ABANDONMENT_ROLES = frozenset(CONFIG['abandonment_roles'])
RESCISSION_ROLE = CONFIG['rescission_role']
OFFICIAL_PATH = Path(__file__).with_name('data') / 'uspto_event_codes.csv'
_official = None


def official_codes():
    """{code: the USPTO's description} from the shipped official list (loaded once)."""
    global _official
    if _official is None:
        with open(OFFICIAL_PATH, encoding='utf-8-sig', newline='') as f: _official = {r['event_cd']: r['event_desc'] for r in csv.DictReader(f)}
    return _official


def role_of(code):
    c = CODES.get(code)
    return c['role'] if c else None


def is_confirmed(code):
    """True when the code is in the official USPTO list AND the tool maps it to a role."""
    c = CODES.get(code)
    return bool(c and c.get('confirmed') and code in official_codes())


def codes_with(role, **kw):
    return tuple(k for k, v in CODES.items() if v['role'] == role and all(v.get(a) == b for a, b in kw.items()))


INTERVIEW_CODES = codes_with('interview')
APPLICANT_INTERVIEW = codes_with('interview', initiator='applicant')   # applicant initiated (own understanding; unconfirmed)
EXAMINER_INTERVIEW = codes_with('interview', initiator='examiner')     # examiner initiated (own understanding; unconfirmed)
AMENDMENT_CODES = codes_with('amendment')
RCE_CODES = codes_with('rce')
APPEAL_CODES = codes_with('appeal')


def statute_level(statute):
    """The parser's statute labels ('112(b)', 'double_patenting_nonstatutory', 'other') at the level the provider data has: 101, 102, 103, 112, dp."""
    s = str(statute or '')
    if s.startswith('112'): return '112'
    if s.startswith('double'): return 'dp'
    return s if s in ('101', '102', '103') else None


def _day(s):
    try: return datetime.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError): return None


def _rank(code):
    r = role_of(code)
    return (PRECEDENCE.index(r) if r in PRECEDENCE else len(PRECEDENCE), code)


def _unanchored(outcome):
    return {'anchor_quality': 'unanchored', 'anchor_code': None, 'anchor_date': None, 'next_event': None, 'next_event_date': None, 'interview_before_next': None,
            'amendment_before_next': None, 'rce_or_appeal_next': None, 'next_event_state': 'unanchored', 'later_events': None, 'abandonment_rescinded': None, 'application_outcome': outcome}


def derive(events, oa_date, oa_code, application_outcome):
    """events: [(date, seq, code), ...] of one application in ANY order. The order the provider gave them in is never used (it is not chronological):
    events are sorted by date and events on the same date are simultaneous. Returns the oa_outcomes values (without the key columns), or None when
    the action has no usable date."""
    od = _day(oa_date)
    if od is None: return None
    ev = sorted(((_day(d), c, d) for d, _s, c in events if _day(d) is not None), key=lambda e: (e[0], str(e[1])))
    ct_code = MAIL_OF.get(oa_code, oa_code)
    mail_code = MAIL_PAIRS[ct_code][1] if ct_code in MAIL_PAIRS else None
    anchor, quality = None, None
    mails = [i for i, (d, c, _) in enumerate(ev) if c == mail_code and abs((d - od).days) <= MAIL_WINDOW] if mail_code else []
    if mails:                                                    # the record date is the mailing date: anchor on the mail event
        anchor = min(mails, key=lambda i: (abs((ev[i][0] - od).days), ev[i][0]))
        quality = 'mail_exact' if ev[anchor][0] == od else 'mail_near'
    else:                                                        # no mail event: the action event itself, which usually sits days before its mailing
        cts = [i for i, (d, c, _) in enumerate(ev) if c == ct_code and -CT_BEFORE <= (d - od).days <= CT_AFTER]
        if cts: anchor, quality = min(cts, key=lambda i: (abs((ev[i][0] - od).days), ev[i][0])), 'ct_near'
    if anchor is None: return _unanchored(application_outcome)
    ad = ev[anchor][0]
    own = {anchor}                                               # the action's own events (its rejection and its mailing): never what came next
    if quality == 'ct_near':
        ms = [i for i, (d, c, _) in enumerate(ev) if mail_code and c == mail_code and 0 <= (d - ad).days <= CT_BEFORE]
        if ms: own.add(min(ms, key=lambda i: ev[i][0]))
    else:
        cs = [i for i, (d, c, _) in enumerate(ev) if c == ct_code and -CT_BEFORE <= (d - ad).days <= CT_AFTER]
        if cs: own.add(min(cs, key=lambda i: (abs((ev[i][0] - ad).days), ev[i][0])))
    later = [e for i, e in enumerate(ev) if e[0] > ad and i not in own]       # strictly after the anchor date; events on the anchor date are not counted
    turn_date = turn = None; skipped = False; resume = None
    for d in sorted({e[0] for e in later}):
        if resume is not None and d < resume: continue
        cand = sorted((e[1] for e in later if e[0] == d and role_of(e[1]) in TURNING_ROLES), key=_rank)
        if not cand: continue
        if role_of(cand[0]) in ABANDONMENT_ROLES:
            undo = [e[0] for e in later if e[0] >= d and role_of(e[1]) == RESCISSION_ROLE]
            if undo: skipped = True; resume = min(undo); continue      # an abandonment later undone is not final: go on to the next turning point
        turn_date, turn = d, cand[0]; break
    between = [e for e in later if turn_date and e[0] <= turn_date] if turn else later          # same-day events count as before the turning point
    state = 'recognised' if turn else ('later_events_none_recognised' if later else 'no_later_event')
    rescinded = 1 if skipped else (0 if turn and role_of(turn) in ABANDONMENT_ROLES else None)
    return {'anchor_quality': quality, 'anchor_code': ev[anchor][1], 'anchor_date': ev[anchor][2][:10], 'next_event': turn, 'next_event_date': turn_date.isoformat() if turn else None,
            'interview_before_next': int(any(role_of(e[1]) == 'interview' for e in between)), 'amendment_before_next': int(any(role_of(e[1]) == 'amendment' for e in between)),
            'rce_or_appeal_next': (int(role_of(turn) in ('rce', 'appeal')) if turn else None), 'next_event_state': state, 'later_events': len(later), 'abandonment_rescinded': rescinded,
            'application_outcome': application_outcome}
