"""
Deprecated shim — tool definitions have moved to app/mcp/tools.py.

The chat assistant (app/ai/chat.py) uses the in-process MCP client
(app/mcp/dispatch.call_in_process) to fetch tool specs and execute tools.
Nothing should import from this module any more.
"""
# This file is intentionally left as a tombstone so that any stale import
# fails with a clear AttributeError rather than silently using a stale copy.
