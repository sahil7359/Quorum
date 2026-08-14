"""Quorum — a citation-grounded, human-approved pull request reviewer.

Layering (enforced by import-linter and by tests/architecture/):

    interface  ->  application | infrastructure  ->  domain

``application`` and ``infrastructure`` are siblings and may not import each other.
``domain`` imports only the standard library. See docs/adr/0001-clean-architecture.md.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
