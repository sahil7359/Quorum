"""Pure text utilities shared across layers.

``estimate_tokens`` lives in ``domain`` rather than beside the chunker because the
application layer needs it too (for context-scoping measurements) and application is
forbidden from importing infrastructure. It is pure arithmetic on a string, so the domain is
the honest home for it.
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Approximate token count at roughly four characters per token.

    Deliberately named *estimate*. It is not a real tokenizer, and nothing that must be
    exact may depend on it. It drives chunk packing and the context-reduction ratio, where
    being 15% out shifts a boundary or a percentage slightly and changes nothing else.

    **Budget accounting never uses this** -- that comes from provider-reported counts on
    ``TokenUsage``, because a cap enforced against a drifting estimate is not a cap.
    """
    return max(1, len(text) // 4)
