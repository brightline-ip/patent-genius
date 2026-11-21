"""Patent Genius MCP server (stdio, newline-delimited JSON-RPC 2.0), standard library only.
Run: python -m pg_tool.mcp_server   (working directory irrelevant). Tools and schemas come from pg_tool/contract.json.
stdout carries protocol messages only; nothing derived from tool input is written to stdout outside a response or to stderr."""
import json, sys
from . import service

SERVER = {'name': 'patent-genius', 'version': service.CONTRACT_VERSION}
DEFAULT_PROTOCOL = '2025-06-18'


def tool_list():
    defs = service.CONTRACT['$defs']; out = []
    for name, t in service.CONTRACT['tools'].items():
        schema = dict(defs[t['input']['$ref'].split('/')[-1]]); schema['$defs'] = defs
        out.append({'name': name, 'description': t['description'] + ' Returns the common Patent Genius envelope (status, data, provenance, caveats, boundary). ' + service.DISCLAIMER + ' ' + service.DRAFT_HEADER + '.',
                    'inputSchema': schema})
    return out


def handle(msg):
    mid, method = msg.get('id'), msg.get('method')
    if method == 'initialize':
        pv = (msg.get('params') or {}).get('protocolVersion') or DEFAULT_PROTOCOL
        return {'jsonrpc': '2.0', 'id': mid, 'result': {'protocolVersion': pv, 'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': SERVER,
                                                         'instructions': 'Patent Genius: public patent research, rejection statistics (strict rule), structural document shells and deterministic office-action assessment. Drafting aid, not legal advice; generated drafts require review by a registered practitioner. Structural checks only; no model interpretation. No tool makes a provider call: live provider search is planned and not part of 0.1.0.'}}
    if method and method.startswith('notifications/'): return None
    if method == 'ping': return {'jsonrpc': '2.0', 'id': mid, 'result': {}}
    if method == 'tools/list': return {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': tool_list()}}
    if method == 'tools/call':
        p = msg.get('params') or {}
        env = service.call(p.get('name'), p.get('arguments') or {})
        return {'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': json.dumps(env, ensure_ascii=False)}], 'structuredContent': env, 'isError': env['status'] == 'error'}}
    if mid is None: return None
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'method not found'}}


def main():
    sys.stdin.reconfigure(encoding='utf-8'); sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    for line in sys.stdin:
        if not line.strip(): continue
        try: msg = json.loads(line)
        except ValueError:
            resp = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            try: resp = handle(msg) if isinstance(msg, dict) else {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'invalid request'}}
            except Exception as e: resp = {'jsonrpc': '2.0', 'id': msg.get('id') if isinstance(msg, dict) else None, 'error': {'code': -32603, 'message': f'internal error ({type(e).__name__})'}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + '\n'); sys.stdout.flush()


if __name__ == '__main__':
    main()
