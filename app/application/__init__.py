"""Application layer: agents, services and use cases.

Depends on ``app.domain`` only. May not import ``app.infrastructure``.

LangGraph is a deliberate, documented exception to "no frameworks here" -- the graph *is*
the application logic. See docs/adr/0002-langgraph-in-application.md. All I/O still arrives
through domain ports; nothing in this package opens a socket, a file or a database session.
"""
