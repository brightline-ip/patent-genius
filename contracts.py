"""Typed structural contracts; these establish no substantive support."""
import re
from prosecution_contracts import document_errors, event_errors, round_errors, claim_version_errors

def manifest_errors(m):
    errors=[]
    def err(s): errors.append('contract: '+s)
    if not isinstance(m,dict): return ['contract: manifest must be object']
    if m.get('schema_version')!=3: err('schema_version must be 3')
    if not isinstance(m.get('documents'),list) or not isinstance(m.get('events'),list):
        return errors+['contract: documents and events must be arrays']
    for d in m['documents']:
        if not isinstance(d,dict): err('document must be object'); continue
        for k in ('id','source_locator','status','document_type','lane','temporal_role'):
            if not isinstance(d.get(k),str) or not d[k]: err('missing/string '+k)
        lane=d.get('lane'); ident=d.get('id','')
        if lane not in ('client_disclosure','public_exemplar','legal_authority'): err('invalid lane')
        prefix={'client_disclosure':'client:','public_exemplar':'public:','legal_authority':'authority:'}.get(lane,'!')
        if not ident.startswith(prefix): err('lane/identifier mismatch')
        if lane=='client_disclosure':
            if not isinstance(d.get('matter'),str) or not ident.startswith('client:'+str(d.get('matter'))+':'): err('matter/identifier mismatch')
            if d.get('temporal_role') not in ('as_filed','later_material','unfiled_disclosure','unknown'): err('invalid client temporal_role')
            if d.get('temporal_role')=='as_filed':
                if not re.fullmatch('[0-9a-f]{64}',str(d.get('original_sha256',''))): err('as-filed original hash required')
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(d.get('filing_date',''))): err('filing date required')
        elif d.get('temporal_role')!='not_client_disclosure': err('public/authority temporal_role invalid')
        for k in ('technologies','cpcs','units'):
            if not isinstance(d.get(k),list): err(k+' must be array')
        if not isinstance(d.get('normalization'),dict) or not isinstance(d.get('normalization',{}).get('warnings'),list): err('normalization warnings required')
        if d.get('status') not in ('ready','needs_ocr','failed_extraction','missing','pending'): err('invalid extraction status')
        if d.get('status')=='ready':
            if not re.fullmatch('[0-9a-f]{64}',str(d.get('sha256',''))): err('ready checksum required')
            if not isinstance(d.get('text_path'),str): err('ready text path required')
            if not d.get('units'): err('ready evidence units required')
        if d.get('normalization',{}).get('warnings'): err('blocking normalization warnings unresolved')
        document_errors(d,err)
        for u in d.get('units',[]) if isinstance(d.get('units'),list) else []:
            if not isinstance(u,dict): err('unit object required'); continue
            if not isinstance(u.get('id'),str) or not isinstance(u.get('source_locator'),str) or not u.get('source_locator'): err('unit ID and exact source locator required')
            if type(u.get('start')) is not int or type(u.get('end')) is not int: err('unit integer offsets required')
    seen=set()
    for e in m['events']:
        if not isinstance(e,dict): err('event object required'); continue
        for k in ('id','application_id','document_id','event_type','date'):
            if not isinstance(e.get(k),str) or not e[k]: err('event '+k+' required')
        if e.get('id') in seen: err('duplicate event');
        seen.add(e.get('id'))
    doc_ids={d.get('id') for d in m['documents'] if isinstance(d,dict)}
    event_errors(m['events'],doc_ids,err)
    for k,fn in (('rounds',round_errors),('claim_versions',claim_version_errors)):
        if k in m and not isinstance(m[k],list): err(k+' must be array'); continue
        fn(m.get(k,[]),doc_ids,m['events'],err) if k=='claim_versions' else fn(m.get(k,[]),m['events'],err)
    return errors

def support_errors(plan,units):
    errors=[]
    def err(s): errors.append('support contract: '+s)
    if not isinstance(plan,dict): return ['support contract: plan object required']
    for k in ('requirements','claims','support_records','issues','required_sections','quotations'):
        if not isinstance(plan.get(k),list): err(k+' must be array')
    if errors: return errors
    if not isinstance(plan.get('matter'),str) or not plan['matter']: err('matter required')
    if not plan['requirements']: err('requirements inventory empty')
    for r in plan['requirements']:
        if not isinstance(r,dict): err('requirement object required'); continue
        if r.get('kind') not in ('disclosure','constraint'): err('requirement kind required')
        if r.get('decision') in ('unknown','omitted') and not r.get('reason'): err('unresolved requirement needs reason')
    if plan.get('claim_scope') not in ('claims_present','no_claims'): err('explicit claim scope required')
    if plan.get('claim_scope')=='claims_present' and not plan['claims']: err('claim inventory empty')
    if plan.get('claim_scope')=='no_claims' and plan['claims']: err('no_claims contradicts inventory')
    records={}; claim_ids=set(); limitations=set()
    for r in plan['support_records']:
        if not isinstance(r,dict): err('support record object required'); continue
        if not r.get('id') or r.get('id') in records: err('missing/duplicate support ID')
        records[r.get('id')]=r
        if r.get('basis') not in ('as_filed','disclosure_only','unknown'): err('explicit support basis required')
        if r.get('status') not in ('mapped_for_review','unknown','omitted'): err('invalid support status')
        if r.get('status')!='mapped_for_review': err(str(r.get('id'))+' unresolved: '+str(r.get('reason','reason missing')))
        if r.get('written_description_review') not in ('not_reviewed','reviewed_by_practitioner') or r.get('enablement_review') not in ('not_reviewed','reviewed_by_practitioner'): err('separate semantic review statuses required')
        if not r.get('evidence'): err('support evidence required')
        for ref in r.get('evidence',[]):
            if ref not in units: err('missing evidence '+str(ref)); continue
            d=units[ref][0]
            if d.get('lane')!='client_disclosure' or d.get('matter')!=plan.get('matter'): err('public/authority/cross-matter support forbidden')
            if r.get('basis')=='as_filed' and d.get('temporal_role')!='as_filed': err('later/unfiled material cannot establish as-filed support')
            if d.get('temporal_role')=='unknown': err('unknown source timing unresolved')
        span=r.get('draft_span')
        if not isinstance(span,list) or len(span)!=2 or any(type(x)is not int for x in span): err('support draft span required')
    for c in plan['claims']:
        if not isinstance(c,dict): err('claim object required'); continue
        ident=c.get('id')
        if not isinstance(ident,str) or ident in claim_ids: err('missing/duplicate claim ID')
        claim_ids.add(ident)
        if c.get('status') not in ('proposed','pending','amended','cancelled'): err('invalid claim status')
        if not isinstance(c.get('depends_on'),list): err('dependency list required')
        if c.get('status')=='cancelled': continue
        if not isinstance(c.get('limitations'),list) or not c['limitations']: err('limitation inventory required'); continue
        expected=[]
        for lim in c['limitations']:
            if not isinstance(lim,dict): err('limitation object required'); continue
            lid=lim.get('id'); expected.append(lid)
            if not isinstance(lid,str) or lid in limitations: err('missing/duplicate limitation ID')
            limitations.add(lid)
            r=records.get(lim.get('support_id'))
            if not r or r.get('kind')!='limitation' or r.get('claim_id')!=ident or r.get('limitation_ids')!=[lid]: err('limitation support record missing/mismatched')
        r=records.get(c.get('combination_support_id'))
        if not r or r.get('kind')!='combination' or r.get('claim_id')!=ident or r.get('limitation_ids')!=expected: err('combination support record missing/mismatched')
    cmap={c.get('id'):c for c in plan['claims'] if isinstance(c,dict)}
    def visit(ident,path):
        if ident in path: err('cyclic claim dependency'); return
        c=cmap.get(ident,{})
        for dep in c.get('depends_on',[]) if isinstance(c.get('depends_on'),list) else []:
            if dep not in cmap or cmap[dep].get('status')=='cancelled': err('missing/cancelled claim dependency')
            else: visit(dep,path|{ident})
    for ident in cmap: visit(ident,set())
    seen=set()
    for issue in plan['issues']:
        if not isinstance(issue,dict): err('issue object required'); continue
        if not issue.get('id') or issue.get('id') in seen: err('missing/duplicate issue ID')
        seen.add(issue.get('id'))
        if issue.get('status')!='mapped_for_review': err('unresolved OA issue '+str(issue.get('id')))
        if issue.get('status')=='mapped_for_review' and not issue.get('draft_span'): err('OA response span required')
    return errors
