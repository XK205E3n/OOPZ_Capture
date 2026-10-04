# Ubuntu 24.04 LTS deployment guide

## Status and boundaries

This implementation targets Ubuntu Server 24.04 LTS x86_64 without a desktop.
It is **not yet accepted for production**. Development and isolated behavioral tests
were performed on Debian 13; these do not certify Ubuntu, systemd, Windows, RTC,
real speech recognition, Feishu delivery, or sustained workload. See
[validation evidence](VALIDATION_UBUNTU.md) for actual results and remaining gates.
The Windows production server is retired. Do not operate unrelated scripts,
kill processes by Python installation path, alter production release directories,
or send production Feishu messages. Retain at least 4 vCPU / 8 GiB RAM and the
80 GiB SSD recommendation (20 GiB free); these have not been lowered.

## Prerequisites (operator on a clean, authorized test machine)

The commands below describe privileged setup; they were not run on production.
This host has a single administrator account, `ubuntu`, shared by every operator
and AI session; **no extra service account is created**. The service runs as
`ubuntu` and is isolated by systemd instead of by a separate login: `NoNewPrivileges=yes`
(the service cannot use sudo or gain privileges), `ProtectHome=yes` (`/home` is
invisible to the service, which hides other projects' data and the SSH keys) and
`PrivateTmp=yes`. Consequently everything the service needs must live under
`/opt/oopz/shared` (its `HOME` is `/opt/oopz/shared/home`), never under `/home/ubuntu`.

```bash
sudo apt-get update
sudo apt-get install python3.12 python3.12-venv fonts-noto-cjk fonts-liberation fontconfig logrotate
```

Provision a supported
Node.js runtime from its official source and verify its published checksum.
Minimum Node is **22.12.0**; it is used only by the Qoder CN CLI (the analyzer), not by the application itself.
When using a Node tarball, put its complete contents (including `bin/node`,
`bin/npm`, `bin/npx`, and `lib/node_modules`) in `/opt/oopz/shared/tools/node`.
Do not link `node` to the tarball root. Preparation prefers that `bin` directory. The selected Node executable is recorded
in the prepared marker and pinned into service OOPZ_NODE_PATH/PATH. Verify `node --version`, `npm --version`, and
`npx --version` under the actual service environment, not only an admin shell.
Do not silently reuse an old system Node. The selected runtime must remain
readable/executable by the service account after activation and rollback.

Install Playwright's system libraries through the administrator-approved package
workflow for the installed Playwright version. The release preparation installs
its matching Chromium as the service user, into `shared/browsers`. No desktop,
Xvfb, physical sound card, PulseAudio or GPU is assumed: the SDK records remote
browser MediaStream tracks. This still requires real browser/RTC testing.
Never add `--no-sandbox` merely to hide an environment permission failure.

The project needs outbound OOPZ API/WebSocket, Agora SDK/RTC, Feishu, the configured
Qoder CN service used by its CLI, and dependency/model registries. There is **no new business inbound
port**. RTC firewall ranges must follow the deployed SDK/provider requirements
and be validated on the test host; HTTPS alone is not a complete RTC test.
No credentials belong in commands, logs, source, release ZIPs or audit reports.

## Analyzer (Qoder CN CLI) and card fonts

The analyzer runs the already-logged-in Qoder CN CLI headless (`-p --tools ""`, a plain
text completion) as the service account; the application stores no analysis API key.
Install it once under `/opt/oopz/shared/tools/` with its own HOME
(`shared/tools/qodercn-home`, holding the login) readable and writable by the service
user, then set `OOPZ_ANALYZER_CLI` and `OOPZ_ANALYZER_HOME` in `shared/config/.env`.
The gateway refuses to start when either is missing. The CLI needs the pinned Node
runtime above.

The digest card is rendered offline with Pillow. The fonts are not in Git or in the
release: run `python scripts/download_fonts.py` (fixed URLs, SHA-256 checked) once into a
shared directory such as `shared/assets/fonts` and set `OOPZ_FONT_DIR` to it.

Chromium remains required by the OOPZ recording SDK; its sandbox/RTC acceptance is
independent (see below).

```bash
sudo bash scripts/linux/install_release.sh prepare \
  --root /opt/oopz --user ubuntu --artifact "$ARTIFACT" --sha256 "$SHA256"
```

Preparation verifies and extracts the full package, builds an independent Python
3.12 environment with CPU speech dependencies,
installs/launch-checks matching Chromium, verifies the pinned model, and records
actual Python/Node versions. It establishes shared config links **before setup**.
It never stops or switches the old service. A failed preparation retains a marked
incomplete directory; retry with the same verified package. Unknown directories
and the current release are never cleaned automatically.

The account defaults to `ubuntu` (`--user ubuntu`). Keep the OOPZ root, HOME, caches,
configuration and state under `/opt/oopz`, separate from unrelated jobs on the host.
Preparation restores `shared/home` even when runuser resets the account's HOME.
If the host's system Python must remain untouched, provision an approved isolated
Python 3.12 runtime and prepend its `bin` directory to PATH before prepare. Keep
that interpreter in persistent storage because release virtual environments use
it. The selected Node directory is placed first by preparation: if system Node is
in `/usr/bin`, this can put system Python ahead of the isolated interpreter. Use
the supported `shared/tools/node/bin` runtime location (or an administrator-managed
link there to an already approved Node), then verify both resolved executables
under the complete preparation environment. Include `/usr/sbin` and `/sbin` in
the operator PATH so `runuser` is available. Do not upgrade an unrelated project's
Python to repair an OOPZ PATH selection problem.

Chromium is installed without the legacy headless shell. The preparation check
explicitly enables `chromium_sandbox=True` and uses the installed `chromium`
channel. If an administrator-managed browser is required, pass its absolute
path in the preparation process's `MD_TO_PDF_CHROME_PATH` (name kept for compatibility); the
check launches that exact executable with sandboxing enabled. No automatic disabling
fallback is provided.

When configuration will be supplied later, stop after prepare and credential-free
checks: imports/pip check, service-user browser and synthetic PCM, Pillow import,
verified model/VAD/public-audio transcription. Leave the gateway inactive and
disabled; do not run setup or activate with an empty configuration. These checks
do not establish live OOPZ/Feishu/API or sustained-load acceptance.

On Ubuntu, a `No usable sandbox` Chromium error must be investigated independently
of the Python browser tests. Different browser drivers can have different sandbox
defaults: the current upstream OOPZ SDK uses Playwright's disabled-sandbox default,
whereas the preparation check enables the browser's normal sandbox. SDK PCM success is
not sandbox readiness evidence. This adaptation does not patch that upstream SDK
behavior. Do not add `--no-sandbox`,
disable AppArmor or globally relax user-namespace restrictions to make a check
pass. Any needed host security-policy change requires explicit approval and must
remain scoped to the verified browser executable.

Configure `shared/config/.env` through the authorized secure operator flow. Do not
paste secrets into chat. `OOPZ_ANALYZER_CLI`/`OOPZ_ANALYZER_HOME` and the login/application
settings are required. For an existing configured app, skip setup. Creating/updating an app and
its persistent permissions requires separate approval and user authorization.

```bash
sudo bash scripts/linux/install_release.sh setup \
  --root /opt/oopz --user ubuntu --release-id "$RELEASE_ID"
```

Setup uses shared config without depending on a current link; activation must
not overwrite credentials written by setup. Verify a dedicated test control group
before activation. Group members have the existing shared control permissions;
this adaptation does not introduce a personal administrator allowlist.

```bash
sudo bash scripts/linux/install_release.sh activate \
  --root /opt/oopz --user ubuntu --release-id "$RELEASE_ID" --enable
sudo systemctl status oopz-capture.service
sudo journalctl -u oopz-capture.service --since today
```

Activation is a real external-service operation. Do not perform it without the
appropriate test account/group authorization. `--enable` opts into boot startup;
otherwise preserve the previous enablement setting. Readiness requires fresh
Feishu-ready output after startup, not an old log line. Inspect both service status
and application logs. A ready gateway alone is not full pipeline acceptance.

## Update, rollback and interrupted transactions

```bash
sudo bash scripts/linux/update_release.sh \
  --root /opt/oopz --user ubuntu --artifact "$ARTIFACT" --sha256 "$SHA256"
sudo bash scripts/linux/rollback_release.sh \
  --root /opt/oopz --user ubuntu --release-id "$PREVIOUS_RELEASE_ID"
```

Before stopping anything, guards reject active tasks, corrupt/illegal PID locks,
symlink locks and unreadable state. Only a valid demonstrably dead PID is stale.
The guard also checks controller `last_job` and the analysis lifecycle's
`analyzing_*`, `preparing_windows` and `building_final_report` stages. A registered
background analysis is busy even before its lock exists. Completed/failed work
and a standalone `prepared` window plan do not alone block a switch. An invalid
controller/status shape is refused instead of being assumed idle.
Do not use `--force` as a routine fix; it explicitly permits interrupting work and
requires operator review of the job and data-recovery consequences.

The switch journal captures actual current-link target, unit/logrotate file
bytes and metadata, and active/enabled service state. Any failure after stopping
must restore those actual snapshots, not a regenerated template. If restoration
fails, the service remains stopped and the journal is retained; never start an
uncertain release. After an abrupt process/host termination, inspect the journal
and recover using the reviewed management script:

```bash
sudo bash scripts/linux/install_release.sh recover --root /opt/oopz --user ubuntu
```

No shared data is rolled back or cleared. Do not manually remove locks or journals
to force a switch. SIGTERM handling drains owned capture/transcription and pending
analysis/delivery work; systemd supplies a bounded stop period and eventual cgroup
cleanup. Test recording, transcription and API-wait interruptions independently.

## Windows data migration

1. Obtain explicit downtime/migration approval; stop the old instance using its
   supported scope. Back up shared config, models, output, feishu_state and logs.
2. Preserve an untouched backup and checksums outside Git/releases. Copy only to a
   separate Ubuntu test/shared tree, never over running production state.
3. Review path-valued config and stored outbox/report absolute paths. Windows drive
   paths do not map automatically to Linux. Reconcile only known fields against
   the old/new roots, preserving IDs, sent/approved markers and report hashes.
4. PID files are machine-local evidence; a coincidental PID on the new host is not
   proof of the old job. Stop both instances and document stale-lock reconciliation;
   malformed locks must not be automatically discarded.
5. Validate reading old sessions, reusing completed analysis, fetching attachments,
   and retrying interrupted work without sending duplicate reports. Do not invoke
   backfill or re-publication merely to test migration.
6. Roll back by stopping the test/new service and restoring the old host/version
   with its original shared backup. Never run two gateways against the same live
   control group/state concurrently.

## Required acceptance record

Record OS/kernel, CPU/memory/swap/disk, dependency freeze, release commit and hash;
then test empty install and failed-prepare retry, real service-user Chromium,
a real digest image from a recorded session, model load/VAD/real audio, approved OOPZ capture and reconnect,
approved analyzer/Feishu image flow, updates/rollback/failure recovery, reboot/enablement,
three shutdown phases and several hours of representative load. Initial target:
chunk close to completed transcription ≤240 s; report actual distributions,
queue growth, dropped chunks and peak resource use. Synthetic/mocked timings are
not capacity proof. Keep Windows regression results separate from Linux tests.
