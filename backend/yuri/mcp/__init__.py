"""MCP connectors — third-party tools Yuri can use.

Spec: docs/superpowers/specs/2026-09-04-yuri-mcp-connectors-design.md

No SDK. MCP's client side is JSON-RPC 2.0 and we need three methods
(initialize, tools/list, tools/call); the `mcp` package reaches this repo only
as a transitive dependency of claude-agent-sdk and renamed its core APIs in
2.x, so owning ~200 lines beats owning that coupling. The wire shape in
jsonrpc.py was captured by hand from a real server, not read from a document.
"""
