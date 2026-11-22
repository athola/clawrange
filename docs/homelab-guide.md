# Homelab Watch: Operator Guide

clawrange watches Home Assistant, Uptime Kuma and the monitoring stack's
router census, and tells you on Telegram only when something needs
attention. This guide covers what it does, why it is safe to run, and how to
switch it on. It assumes you are comfortable with Docker Compose and Home
Assistant. Every address below is a placeholder.

With no homelab configuration set, the stack behaves exactly as before: an
unset source is skipped silently.

## What It Does

| Surface | When | Content |
|---|---|---|
| Alerts | `homelab_watch`, every 5 min, only on a change | new device (universally administered MAC), router management ports differ from the learned baseline, new MikroTik KEV entry, RouterOS version change, census older than 13 h, Kuma monitor up/down, HA entity `unavailable` for 30 min (`unknown` is report-only; entities already down at baseline wait until they recover and fail again; more than 10 at once collapse into one line, and so do more than 10 new devices), a source failing 3 times in a row and its recovery |
| Daily report | `homelab_report`, once a day (re-runs after a missed day) | low batteries (< 20 %), pending `update.*` entities, randomized-MAC count, Kuma 24 h uptime, the MikroTik KEV list |
| `!home` | on demand | HA summary; `!home <text>` searches entities (15 lines max) |
| `!net` | on demand | census, router ports, RouterOS version, KEV, Kuma |
| `!home do <domain.service> <entity_id>` | on demand | asks to run one allowlisted HA action; the confirmation code arrives as a separate message |
| `!home confirm <code>` | within 120 s | runs that action |

The first successful read of each source learns a baseline and sends no
alerts. A failed read never counts as a change, so an outage does not read
as "every device left". KEV entries carry no affected-version field, so a KEV
alert names the CVE and the running RouterOS version and points you at
MikroTik's advisory. It never says the router is vulnerable.

## Trust Model

- **Pull-only.** clawrange runs in the secure zone and pulls from HA, Kuma
  and the scanner. Nothing in the IoT zone can open a connection to
  clawrange, and nothing is added that would need one: no HA webhooks,
  `rest_command` or MQTT bridge.
- **No router login.** clawrange reads the scanner's JSON
  (`http://scanjson/*.json`). The scanner stays the only SSH client to the
  router and the only fetcher of the CISA KEV feed. RouterOS REST stays off.
- **`HA_TOKEN` lives only in the workflows container.** OpenClaw never sees
  it. It belongs to a dedicated HA user (`clawrange`). Never reuse a token
  you already use for anything else.
- **Network detail stays out of git, brain pages and LLM prompts.**
  Hostnames and entity names are attacker-chosen strings. They are
  sanitized, length-capped and rendered by deterministic code only. Learned
  state lives in the brain DB table `homelab_state`, never in brain pages.
- **Kuma TLS is verified** against the Pi's root CA. Verification is never
  turned off.
- **Actions are denied by default.** The allowlist
  (`homelab.home_assistant.actions`) starts empty. These domains are
  hard-denied in code whatever the profile says, checked against both the
  service domain and the entity domain: `lock`, `cover`,
  `alarm_control_panel`, `valve`, `siren`, `camera`, `homeassistant`,
  `hassio`, `shell_command`, `rest_command`, `python_script`, `pyscript`,
  `recorder`, `system_log`. `script.*` and `automation.*` globs fail profile
  validation, so name scripts by exact entity id.
- **Confirmation codes bypass the model.** `!home do` and `!home confirm`
  must each be the whole message. The 6-digit code goes straight to Telegram
  from workflows, never through the assistant's reply. It is valid for
  120 s and works once.
- **Public health is coarse.** `GET /healthz/homelab` shows per source
  `configured`, `ok` (`null` when unconfigured), `last_success`,
  `failures` and `incomplete` (a URL without its token, for example), plus
  top-level `status`, `problems` (names only), `census_when` and
  `last_run`. It goes `degraded` when a source fails, is incomplete, or a
  census file is stale, or when no watch ran in 15 minutes. It never shows
  URLs; error text and census `part_errors` need the bearer token.
- **Outages are outages.** A census with no lease/ARP table, a Kuma reply
  without heartbeats, or an HTTP 401 count as a failing source (the down
  alert says "rejected the credentials" for 401/403), never as an empty
  house. Alerts are stored before sending and retried from an outbox.
- **Census network.** `scanjson` joins `clawrange-homelab`, an internal
  network that only the workflows container is on. OpenClaw (whose agents
  have `web_fetch`) and DeerFlow are not, so the census never reaches a model.

## Activation Runbook

Run these in order. `<monitoring>` is the monitoring stack checkout, and
`<pi-ip>` is the Pi's address. Steps 2, 3, 5 and 7 run in the clawrange
checkout.

1. **Create the HA user and token.** In HA, open Settings > People > Users
   > Add user, with name and username `clawrange`. If the Users tab is
   missing, first enable Advanced mode in your own profile. Store the
   password in your password manager. Leave Administrator off. HA core
   serves `/api/states` to non-admin users (filtered to what they may
   read), and a long-lived token carries its owner's permissions. While
   the actions allowlist is empty, a user in HA's read-only group
   (`system-read-only`) would make HA refuse writes on its own side too.
   The UI may not offer that group; if it does not, the empty clawrange
   allowlist is the only write barrier, which is the design default. Use
   `system-users` once you add an action rule. Log in as
   `clawrange` in a private window, then open
   Profile > Security > Long-lived access tokens > Create token. Copy the
   token once and log out. Verify the token:

   ```sh
   read -rs HA_TOKEN   # paste the token, press Enter; keeps it out of history
   curl -s -o /dev/null -w '%{http_code}\n' \
     -H "Authorization: Bearer $HA_TOKEN" http://<pi-ip>:8123/api/states
   unset HA_TOKEN
   ```

   A `200` means keep the user non-admin. Only if this returns `401` or
   `403`, turn on Administrator for `clawrange` and run it again. A `401`
   after that means the token is wrong.

2. **Copy the Pi's root CA** (public certificate only) into clawrange:

   ```sh
   mkdir -p config/homelab
   cp <monitoring>/dashboard/homepage/config/pi-root-ca.crt config/homelab/
   ```

   Never copy the CA's private key. The root is name-constrained to the
   Pi's IP, so trusting it cannot vouch for any other site.
   `config/homelab/` is gitignored and mounted read-only at
   `/config/homelab`.

3. **Configure clawrange.** Add to `.env`:

   ```sh
   HA_URL=http://<pi-ip>:8123
   HA_TOKEN=<token from step 1>
   HOMELAB_KUMA_URL=https://<pi-ip>:8444
   HOMELAB_CA_CERT=/config/homelab/pi-root-ca.crt
   HOMELAB_CENSUS_URL=http://scanjson
   ```

   Use the IP in `HOMELAB_KUMA_URL`. The Pi's certificate names only its IP,
   so a hostname fails verification. The active profile (`CLAWRANGE_PROFILE`)
   must define a `homelab` block. Without one, `/healthz/homelab` and
   `/homelab/*` are not mounted (404) and nothing runs. The `marketing`
   profile ships this block; copy it into any other profile you run.
   Unknown keys fail at load time.

   ```yaml
   homelab:
     home_assistant:
       url: ${HA_URL}
       token: ${HA_TOKEN}
       battery_low: 20
       unavailable_after_minutes: 30
       flood_threshold: 10
       ignore_entities: []
       actions: []
     kuma:
       url: ${HOMELAB_KUMA_URL}
       slug: home
       ca_cert: ${HOMELAB_CA_CERT}
     census:
       url: ${HOMELAB_CENSUS_URL}
       max_age_hours: 13
     source_down_after: 3
   ```

4. **Swap in the prepared monitoring config, then rebuild.** The live
   `compose.yaml` and `homepage/config/services.yaml` were left as they
   were; the homelab versions sit beside them as `*.homelab-ready`. The
   prepared compose declares clawrange's `clawrange-homelab` network as
   external, so that network must exist first (it appears once clawrange
   runs this branch's compose):

   ```sh
   docker network inspect clawrange-homelab >/dev/null && echo "network ok"
   cd <monitoring>/dashboard
   cp compose.yaml.homelab-ready compose.yaml
   cp homepage/config/services.yaml.homelab-ready homepage/config/services.yaml
   docker compose up -d --build scanner scanjson homepage
   ```

   Rollback for this step: copy each `*.bak-2026-11-21` back and run
   `docker compose up -d` again.

   If the network is missing, do step 5 first and come back. The scanner
   runs its legs as soon as it starts. Check them:

   ```sh
   docker logs --tail 20 scanloop                    # no "failed" lines
   curl -s 127.0.0.1:3200/router.json | jq -r .when
   curl -s 127.0.0.1:3200/kev-mikrotik.json | jq '.entries | length'
   ```

5. **Start clawrange.** In the clawrange checkout, run `make start` (dev) or
   `make start-prod` (prod, which rebuilds the image). Both recreate workflows with the new
   environment.

6. **Verify.**

   ```sh
   curl -s localhost:5678/healthz/homelab | jq .
   ```

   Expect `status: "ok"`, and `configured: true` and `ok: true` for `ha`,
   `kuma` and `census` once the first watch has run (within 5 min).
   `POST /homelab/watch` with the bearer token runs one immediately. Then
   send `!net` and `!home` in Telegram. The first run only learns baselines,
   so no alerts are expected. The Homepage card "Homelab watch (clawrange)"
   (Compute & AI) should read `ok` for each source.

7. **Optional: allow a few actions.** Add rules to the profile's
   `homelab.home_assistant.actions`, then restart workflows
   (`docker compose restart workflows`) so the profile reloads:

   ```yaml
   actions:
     - service: script.turn_on
       entities: [script.<announce_script_id>]   # exact id; script.* is rejected
     - service: light.turn_on
       entities: ["light.*"]
     - service: light.turn_off
       entities: ["light.*"]
   ```

   Send `!home do light.turn_on light.<entity_id>`. A code arrives as its own
   message. Reply `!home confirm <code>` within 120 s. With a non-admin HA
   user, try one allowed action once to confirm HA permits the call.

## Rollback

To switch the watch off, remove (or empty) the five env vars from `.env`
and run `make start`. Every source becomes unconfigured and goes silent.
Alternatively, remove the `homelab` block from the profile, which also
unmounts the routes. Either way the stack runs as it did before. Learned
baselines stay in `homelab_state`, where they do nothing. Revoke the token
by logging in to HA as `clawrange` and deleting it under Profile > Security,
then deactivate the user. To undo the monitoring side, restore
`<monitoring>/dashboard/compose.yaml.bak-2026-11-21` (and the scanner
scripts' `.bak-2026-11-21` copies if you want) and rerun step 4's compose
command. The extra `router.json` and `kev-mikrotik.json` files are harmless
if left. While `scanjson` is attached to `clawrange-homelab`, stopping clawrange
cannot remove the network: `make stop-prod` prints "has active endpoints"
and `make stop` hides the error. That is expected, and the next start reuses
the network.
