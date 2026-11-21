"""Launcher for the Patent Genius MCP server from any working directory:
  python "<PATENT GENIUS>/pg_tool_mcp.py"          (MCP stdio server)
  python "<PATENT GENIUS>/pg_tool_mcp.py" --cli ... (same as python -m pg_tool.cli ...)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == '__main__':
    if sys.argv[1:2] == ['--cli']:
        from pg_tool.cli import main
        sys.exit(main(sys.argv[2:]))
    from pg_tool.mcp_server import main
    main()
