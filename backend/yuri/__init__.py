"""Yuri — the control plane above coding agents. New code only; the existing
Yapcode modules (tools, session_manager, *_runner) stay where they are and call
into this package. Layers, top-down: api → services → domain/store/events →
providers. Providers never import the store or the domain."""
