# shellcheck shell=bash
# Sourced (not executed) by scripts that call protected workflows routes.
# Every workflows route except the GET health, /tier and /sched reads requires
# `Authorization: Bearer $PROXY_AUTH_TOKEN`. The header is written to a
# 0600 temp file and passed as `curl -H @"$WF_AUTH_HEADER"`, so the token never
# appears on a command line (ps) or in script output.
#
# Usage (after sourcing .env):
#   . ./scripts/workflows_auth.sh
#   workflows_auth_init || exit 1
#   curl -H @"$WF_AUTH_HEADER" ...

WF_AUTH_HEADER=""

workflows_auth_cleanup() {
  if [ -n "$WF_AUTH_HEADER" ]; then
    rm -f "$WF_AUTH_HEADER"
  fi
}

workflows_auth_init() {
  # The API also rejects the .env.example placeholder (workflows/auth.py).
  if [ -z "${PROXY_AUTH_TOKEN:-}" ] || [ "${PROXY_AUTH_TOKEN}" = "generate-a-token-here" ]; then
    echo "FAIL: PROXY_AUTH_TOKEN is empty or the .env.example placeholder. Set a" >&2
    echo "      real token in .env; the workflows API returns 401 without" >&2
    echo "      'Authorization: Bearer \$PROXY_AUTH_TOKEN'." >&2
    return 1
  fi
  WF_AUTH_HEADER=$(umask 077 && mktemp "${TMPDIR:-/tmp}/clawrange.XXXXXX")
  trap workflows_auth_cleanup EXIT
  printf 'Authorization: Bearer %s\n' "$PROXY_AUTH_TOKEN" > "$WF_AUTH_HEADER"
}
