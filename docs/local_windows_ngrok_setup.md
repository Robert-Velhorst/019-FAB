# FAB Local Windows 11 and ngrok Setup

FAB is local-first. The supported Windows runtime is the authenticated API, autonomous worker, and production operator dashboard started together by the repository launcher.

## Local Windows 11 mode

1. Install Python 3.13, Node.js, pnpm, Tesseract OCR with Dutch and English language data, and Poppler PDF tools. The launcher creates and validates an isolated `.venv`; it does not install packages into the global Python runtime.
2. Create `config/config.ini` from `config/config_template.ini`.
3. Set a random `operations.api_token` of at least 32 characters. Do not paste it into URLs, browser storage, logs, or Git.
4. Start FAB:

```powershell
.\Start-FAB.ps1 -NoBrowser
```

5. Run the sanitized readiness check with FAB's isolated runtime:

```powershell
.\.venv\Scripts\python.exe -m src.run_fab_doctor
```

The first start installs the local Python requirements into `.venv` and the dashboard packages into `web/node_modules` when they are absent. FAB can bootstrap Python 3.13 through the Windows Python launcher, `uv`, or a matching system `python`. It binds that private environment to the SHA-256 checksum of `requirements-local.txt`; when the dependency contract changes, the launcher stops FAB and rebuilds only this checkout's verified, non-linked `.venv`. This removes packages that are no longer part of FAB without modifying the system Python installation. The launcher prints the selected URLs. Defaults are:

- Operator dashboard: `http://127.0.0.1:3000/admin/operations`
- Local API: `http://127.0.0.1:5001`
- Authenticated constant-time liveness: `GET /api/live`
- Authenticated deep health: `GET /api/health`

Readiness, doctor output, and sanitized support bundles use the active React dashboard URL only after its loopback identity endpoint proves the same checkout and exact API origin. A stale, malformed, mismatched, or unreachable launcher runtime file is ignored and diagnostics fall back to the ledger API dashboard. Compose supplies the operator URL explicitly through `FAB_OPERATOR_DASHBOARD_URL`.

API clients use `Authorization: Bearer <token>`. The API runs under Waitress with bounded threads; the worker and dashboard are separate managed processes. `Stop-FAB.ps1` stops only the FAB instance recorded for this checkout.

## Managed ngrok access

The managed launcher exposes only the authenticated FAB API. The operator dashboard remains on loopback. Start FAB first, configure ngrok for the Windows user, and then run:

```powershell
.\Start-FAB-Ngrok.cmd
```

Run the ngrok launcher and verifier with the same configuration/environment used
to start FAB. Both resolve the configured operator and separate HAI credentials,
including `FAB_LOCAL_API_TOKEN_FILE` and `FAB_HAI_API_TOKEN_FILE`. Configured values
take precedence over encrypted stored credentials. When configuration omits a
credential, they can read the existing encrypted local store, but never create or
rotate a credential. Missing, invalid or identical operator/HAI credentials block
the tunnel before any public endpoint is started. The `local` profile retains the
main launcher's stored-credential fallback for short legacy configured values;
an explicit weak `windows` credential is rejected instead.

If configuration changed since FAB started, these scripts cannot recover the
running process's former credentials. Restore its startup configuration or perform
an explicit owned stop/start with the intended settings. Do not replace tokens or
disable authentication simply to make a tunnel check pass.

When another ngrok endpoint is already online, FAB refuses to stop or pool it. Reserve a separate HTTPS endpoint for FAB and pass its clean origin explicitly:

```powershell
.\Start-FAB-Ngrok.cmd -Url https://your-reserved-endpoint.example
```

Startup succeeds only after all of these checks pass:

- the local API belongs to this checkout and requires a strong bearer token;
- an unauthenticated remote `/api/live` request receives `401`;
- authenticated liveness returns this checkout's FAB instance identity;
- the authenticated HAI manifest is available remotely;
- the private ngrok inspector shows the exact endpoint forwarding to FAB's loopback API.

The dashboard connection panel and authenticated `GET /api/cloud/status` report `active` only while that owned runtime remains verifiable. Stop the endpoint with:

```powershell
.\Stop-FAB-Ngrok.cmd
```

`Stop-FAB.cmd` also stops the managed tunnel before the local API. Both stop paths require the recorded project identity, ngrok PID, `fab-managed` command marker, and private overlay path to match, so a stale PID cannot stop another process.

## Temporary ngrok verification

Start FAB locally, configure ngrok for the Windows user, then run:

```powershell
.\Test-FAB-Ngrok.ps1
```

The verifier creates an isolated temporary agent, exposes only the local API port, proves an unauthenticated request receives `401`, verifies authenticated liveness and the HAI manifest, and then removes its temporary files and endpoint. It uses FAB's isolated Python runtime and leaves no persistent cloud status.

If the ngrok account requires a second reserved endpoint, supply it explicitly:

```powershell
.\Test-FAB-Ngrok.ps1 -Url https://your-reserved-endpoint.example
```

`ERR_NGROK_334` means the account already has an endpoint online. Do not stop or pool an unrelated endpoint; reserve a separate FAB endpoint and rerun with `-Url`. Both the managed launcher and verifier now detect the common local conflict before starting another agent.

## Exposure rules

- Never expose FAB without a strong API token.
- Keep the API and dashboard listeners on loopback unless an explicitly reviewed reverse proxy is used.
- Do not put credentials, document identifiers, or tokens in a public URL.
- Treat ngrok as supervised remote access, not as provider acceptance or a substitute for monitored production hosting.
- Stop managed access when it is not needed. Temporary verification stops itself.
