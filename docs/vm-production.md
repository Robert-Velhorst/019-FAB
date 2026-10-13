# Single-Business VM Deployment

This profile runs one business on one Linux Docker host. The SQLite ledger and
worker share a durable volume. It is not a multi-host or multi-tenant deployment.

## Configuration

Use `docker-compose.vm.yml` alone. The ordinary Compose file remains the local
desktop profile and must not be merged into this one.

Provision three independent random secrets with at least 32 characters: an API
token, a browser-session signing secret, and the operator sign-in secret. Keep
them in separate files outside the repository. Set these environment variables
to their absolute host paths:

```text
FAB_API_TOKEN_FILE
FAB_SESSION_SECRET_FILE
FAB_OPERATOR_TOKEN_FILE
```

The files are mounted read-only as Docker secrets. Compose file mounts do not
encrypt host files or guarantee ownership changes: provision read permissions
for the container service users (API UID 10001, Node UID 1000), restrict host
directory access, and verify access on the target host. Never make the secret
directory publicly readable. The API token is consumed by API, worker and web;
the session and operator secrets are consumed only by web.

Configure these non-secret settings in an ignored environment file outside Git:

```text
FAB_OPERATOR_PUBLIC_ORIGIN=https://fab.example.org
FAB_LEDGER_PUBLIC_ORIGIN=https://ledger.example.org
FAB_TRUSTED_PROXY_ADDRESSES=<actual proxy peer IP as seen by the Node container>
FAB_INTAKE_DIR=<absolute host directory containing documents>
FAB_STORAGE_ENCRYPTION_CONFIRMED=false
```

Use two distinct owned HTTPS origins: the dashboard and authenticated ledger.
Set the encryption attestation to `true` only after checking encryption for the
ledger, backup, and secret volumes. This is an operator statement, not automatic
verification. Provider secrets and OAuth tokens persist in `fab-credentials` and
`fab-tokens`, shared by API and worker but not web. These volumes must also be
encrypted and protected by host permissions. Desktop Google consent/setup routes
require a loopback-bound API and are not enabled by the VM wildcard bind. Provision
owner-authorized credentials separately; VM Google onboarding is not yet proven.

## Start and Sign In

```sh
docker compose --env-file /etc/fab/deployment.env -f docker-compose.vm.yml config --quiet
docker compose --env-file /etc/fab/deployment.env -f docker-compose.vm.yml up -d --build
docker compose --env-file /etc/fab/deployment.env -f docker-compose.vm.yml ps
```

Only loopback host ports 3000 and 5001 are published. A host reverse proxy must
terminate TLS and overwrite forwarding headers. Start with
[`deploy/nginx.vm.conf.example`](../deploy/nginx.vm.conf.example), replace the
names/certificates, and validate the proxy configuration before enabling it.
Do not use a proxy-hop count or trust arbitrary forwarding headers. Set the
explicit observed proxy IP address; Docker may change the gateway when its
network is recreated. Keep Docker engine access restricted to administrators.

Open `https://fab.example.org/operator/login` and enter the operator secret.
Remote sessions expire, and restart or credential rotation revokes them. Open
`/operator/logout` to sign out. The underlying API token never enters this form.
A managed ledger handoff binds its Flask session to the issuing dashboard session.
Each cookie-authenticated ledger request checks that dashboard session again.
Dashboard logout, expiry, restart or signing/operator-secret rotation therefore
denies subsequent linked ledger requests, including replay of saved cookies.
An already authorized in-flight action is not cancelled retroactively.

Compose supplies `FAB_OPERATOR_SESSION_VALIDATION_URL` to Python as
`http://web:3000/api/fab/operator-session/status`. Keep this direct internal address;
it does not need the public reverse proxy. The status credential is purpose-derived
from the shared API token, never the browser password or cookie. This route does
not accept the raw API or HAI token. Validation does not cache successful results;
if web is unavailable, linked browser access fails closed and the user signs in
again after recovery. API/worker health checks use bearer authentication and do
not depend on web, avoiding a startup dependency cycle.

Run one Node web process for this profile. Session and consumed-handoff records
are bounded in memory; Node restart revokes outstanding authority. A consumed
handoff cannot be reused after a Flask-only restart. Independent API bearer and
explicit master-token ledger logins remain separate credentials, not descendants
of dashboard login. Sign those out separately on shared devices. HAI keeps its
own restricted route permissions even when a ledger browser cookie is present.
The VM dashboard uses one administrator identity. It does not yet implement a
separate restricted team-operator role.

## Health and Recovery

API health probes check authenticated liveness. Worker probes check that the
owned process still exists inside its container; workflow stalls and provider
failures are diagnosed in the dashboard. Docker does not restart a process just
because its health status is unhealthy; supervise unhealthy alerts through the
host monitoring service. `restart: unless-stopped` handles process exits.

Container limits default to 2 CPUs/2 GiB for each Python service and 1 CPU/512 MiB
for web. OCR workloads may require tuning based on measured document size. Logs
rotate at 10 MiB, keeping three files per container. Source files mount read-only.
Scheduled recovery packages use the separate `fab-backups` volume. Migration
snapshots and restored evidence remain on `fab-data`; recovery staging uses the
container temporary filesystem. Encrypt the host storage covering all of them.
Copy verified recovery packages
to separately encrypted off-host storage to survive loss of the VM itself.

Run a disposable recovery rehearsal without selecting a real ledger:

```sh
docker compose --env-file /etc/fab/deployment.env -f docker-compose.vm.yml run --rm --no-deps api python -m src.run_recovery_rehearsal
```

Before upgrading, create and verify a source-complete recovery package, stop
the worker, and record the deployed source revision or image digest. Retain the
previous images. Run the new images against an isolated restored copy before
changing the live stack. If a schema changed, rollback requires the verified
pre-upgrade recovery package, not just an older image.

## Rotation

Rotate one credential at a time during a maintenance window. For the API token,
replace its mounted file and recreate API, worker and web together so they use
the same value. For the session or operator secret, replace its file and recreate
web. Verify old sessions fail and new sign-in succeeds before resuming operation.
Rotation never automatically rotates Google or Wave credentials.

Production acceptance still requires target-host TLS, volume encryption, secret
permissions, off-host backup monitoring, recovery rehearsal, and authorised live
provider readback. See the implementation verification report for checks actually
run in development.
