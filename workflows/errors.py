"""Shared exception types.

An upstream failure (a scanner API, the LLM tiers, a web fetch) must not
look like an empty result: callers that cannot tell "nothing found" from
"could not look" report failures as success. Raise ``UpstreamError`` for
the latter and let the caller decide how to surface it.
"""


class UpstreamError(RuntimeError):
    """An external dependency failed, so no result is available."""
