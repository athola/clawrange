"""Homelab integration: a pure functional core for watching the home.

Everything here is side-effect free (no network, DB, env or Telegram); the
imperative shell injects observations and the clock. See
``docs/homelab-integration-plan.md``.
"""

# First line of every !home / !net reply. llm_proxy drops assistant turns that
# carry it from the history it forwards upstream: replies hold entity names and
# hostnames that are attacker-chosen and must never reach a model.
HOMELAB_REPLY_MARKER = "[homelab]"
