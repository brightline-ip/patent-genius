"""Patent Genius tool CLI: the same tools and envelope as the MCP server, without an LLM.
  python -m pg_tool.cli list
  python -m pg_tool.cli <tool> --input '{"key": "103"}'      (or --input @request.json, or --input - for stdin)
  python -m pg_tool.cli pg_report --input @req.json --markdown   (prints only the report Markdown)
Exit codes: 0 ok/partial, 3 not_found, 4 deferred, 1 error, 2 usage."""
import argparse, json, sys
from . import service


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(prog='python -m pg_tool.cli', description='Patent Genius tool service CLI. ' + service.DRAFT_HEADER + '.')
    ap.add_argument('tool', help='tool name, or "list"')
    ap.add_argument('--input', default='{}', help='JSON request, @file, or - for stdin')
    ap.add_argument('--markdown', action='store_true', help='pg_report: print only data.markdown')
    a = ap.parse_args(argv)
    if a.tool == 'list':
        print('# ' + service.DRAFT_HEADER)
        for n, t in service.CONTRACT['tools'].items(): print(f"{n}\t{t['description']}")
        return 0
    raw = sys.stdin.read() if a.input == '-' else open(a.input[1:], encoding='utf-8').read() if a.input.startswith('@') else a.input
    try: req = json.loads(raw)
    except ValueError: print(json.dumps({'status': 'error', 'error': {'code': 'invalid_input', 'message': 'input is not JSON'}})); return 2
    env = service.call(a.tool, req)
    if a.markdown and a.tool == 'pg_report' and env['status'] in ('ok', 'partial'): print(env['data']['markdown'])
    else: print(json.dumps(env, indent=1, ensure_ascii=False))
    return {'ok': 0, 'partial': 0, 'not_found': 3, 'deferred': 4}.get(env['status'], 1)


if __name__ == '__main__':
    sys.exit(main())
