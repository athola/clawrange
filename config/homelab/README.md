# Homelab certificates and private settings

The directory is mounted read-only into the workflows container. Everything
in it except this README is gitignored.

- **Kuma CA.** Put the CA certificate that signs Uptime Kuma's TLS here (the
  Pi's root CA, a `.pem` or `.crt` file) and set
  `HOMELAB_CA_CERT=/config/homelab/<file>` in `.env`. Public certificates
  only: never put a private key here.
- **Private HA settings.** Real entity ids do not belong in the committed
  profile. Put them in `ha-local.yaml` and set
  `HOMELAB_HA_LOCAL=/config/homelab/ha-local.yaml`. The file may hold only
  `ignore_entities` and `actions` (same format as the profile's
  `homelab.home_assistant`), and both are added to the profile's lists.
  A file that is missing or invalid is ignored as a whole: none of its
  actions are allowed, and `/healthz/homelab` reports `ha_local_file`.
