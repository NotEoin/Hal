"""The Hal agent: model providers, the agent loop, tool-call repair, coding and assistant
toolsets, sessions, compaction, AGENTS.md and skills, the MCP client and the ACP agent server.

It has no UI and no host side effects of its own. It reaches host tools only through MCP on the
broker socket and never imports `halhub` (resolution A2). Design: docs/design/03-agent.md.
This package is a skeleton: the first behaviour lands in phase 4 of the implementation plan.
"""

__version__ = "0.1.0.dev0"
