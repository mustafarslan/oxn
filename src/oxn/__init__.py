"""OXN -- a local architecture and quality gatekeeper for LLM coding agents.

This module is imported by every entry point, including the PostToolUse hook that runs
after every agent edit. Keep it free of imports: see ADR-0002 for the latency budget
(p95 <= 200 ms per file, of which a bare interpreter already costs ~15 ms).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
