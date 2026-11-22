# Homelab certificates

Put the CA certificate that signs Uptime Kuma's TLS here (the Pi's root CA,
a `.pem` or `.crt` file) and set `HOMELAB_CA_CERT=/config/homelab/<file>` in
`.env`. The directory is mounted read-only into the workflows container.
Public certificates only: never put a private key here. Everything in this
directory except this README is gitignored.
