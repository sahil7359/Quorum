"""Domain layer: entities, value objects and Protocol ports.

This package imports **only** the standard library. No framework, no driver, no client.
The rule is enforced twice, on purpose:

* an ``import-linter`` forbidden contract in ``pyproject.toml``, and
* ``tests/architecture/test_domain_is_pure.py``, which parses the AST of every module here.

The second exists because the first is configuration, and configuration can be relaxed in
the same commit that violates it.
"""
