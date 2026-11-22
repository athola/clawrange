"""Homelab integration: a pure functional core for watching the home.

Everything here is side-effect free (no network, DB, env or Telegram); the
imperative shell injects observations and the clock. See
``docs/homelab-integration-plan.md``.
"""
