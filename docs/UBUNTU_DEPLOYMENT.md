# Ubuntu 24.04 LTS installation and acceptance

## Status and boundaries

This implementation targets Ubuntu Server 24.04 LTS x86_64 without a desktop.
It is **not yet accepted for production**. Development and isolated behavioral tests
were performed on Debian 13; these do not certify Ubuntu, systemd, Windows, RTC,
real speech recognition, Feishu delivery, or sustained workload. See
[validation evidence](UBUNTU_VALIDATION.md) for actual results and remaining gates.
Keep Windows production stopped for this task. Do not operate unrelated scripts,
kill processes by Python installation path, alter production release directories,
or send production Feishu messages. Retain at least 4 vCPU / 8 GiB RAM and the
80 GiB SSD recommendation (20 GiB free); these have not been lowered.

## Prerequisites (operator on a clean, authorized test machine)

The commands below describe privileged setup; they were not run on production.
Use an existing dedicated unprivileged service account, or have the administrator
create one. Do not use the login account that runs unrelated jobs.

```bash
sudo apt-get update
sudo apt-get install python3.12 python3.12-venv fonts-noto-cjk fonts-liberation fontconfig logrotate
sudo useradd --system --create-home --home-dir /opt/oopz/shared/home --shell /usr/sbin/nologin oopz
```

Only run useradd if the account does not already exist. Provision a supported
Node.js runtime from its official source and verify its published checksum.
Minimum Node is **22.12.0**, as required by the locked PDF dependency tree.
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
analysis API, and dependency/model registries. There is **no new business inbound
port**. RTC firewall ranges must follow the deployed SDK/provider requirements
and be validated on the test host; HTTPS alone is not a complete RTC test.
No credentials belong in commands, logs, source, release ZIPs or audit reports.

## Release provenance and layout

Formal artifacts still come only from `scripts/build_release.ps1` on a clean,
committed HEAD, after tests and release audit. A source checkout or test fixture
ZIP is not a formal release. Obtain the SHA-256 through a trusted release channel;
a checksum downloaded from an untrusted source beside an archive is not proof of
its authenticity. Use reviewed bootstrap scripts to validate the archive before
executing anything from it. The updater extracts the complete verified archive,
including all installer helpers, rather than extracting only install_release.sh.

```
/opt/oopz/
  releases/<release-id>/       # immutable code and independent .venv/node_modules
  current -> releases/<id>
  shared/config/.env           # credentials and persistent configuration
  shared/models/              # verified model files
  shared/output/              # sessions, transcripts and reports
  shared/feishu_state/         # control state/outbox/audit
  shared/logs/                # persistent runtime logs
  shared/browsers/            # service-user Playwright cache
  shared/home/                # fixed HOME and user caches
  shared/tools/node/          # complete Node runtime
  artifacts/                 # verified release downloads
```

Release `.env`, models, output, feishu_state and logs point into shared. Do not
replace shared .env when upgrading. Configuration path precedence is explicit
function argument, then OOPZ_ENV_FILE, then release .env; existing process
environment values take precedence over file values. Relative config-file paths
are relative to the release root. Avoid setting duplicate runtime configuration
in systemd, since it would override later in-file changes. Settings writers retain
in-place semantics for both Windows hardlinks and Linux symlinks.

## First installation: prepare → configure/setup → activate

Examples use reviewed bootstrap scripts in an operator-owned checkout. Replace
ARTIFACT, SHA256 and RELEASE_ID with values from your verified artifact. Scripts
are explicitly invoked with bash; ZIP executable bits are not required.

```bash
sudo bash scripts/linux/install_release.sh prepare \
  --root /opt/oopz --user oopz --artifact "$ARTIFACT" --sha256 "$SHA256"
```

Preparation verifies and extracts the full package, builds an independent Python
3.12 environment with CPU speech dependencies, installs frozen Node dependencies,
installs/launch-checks matching Chromium, verifies the pinned model, and records
actual Python/Node versions. It establishes shared config links **before setup**.
It never stops or switches the old service. A failed preparation retains a marked
incomplete directory; retry with the same verified package. Unknown directories
and the current release are never cleaned automatically.

Configure `shared/config/.env` through the authorized secure operator flow. Do not
paste secrets into chat. Keep the existing provider semantics: target DeepSeek
Flash, thinking enabled, provider-default effort; no prompt rewrite or model
substitution. All required ANALYZER fields and login/application settings still
apply. For an existing configured app, skip setup. Creating/updating an app and
its persistent permissions requires separate approval and user authorization.

```bash
sudo bash scripts/linux/install_release.sh setup \
  --root /opt/oopz --user oopz --release-id "$RELEASE_ID"
```

Setup uses shared config without depending on a current link; activation must
not overwrite credentials written by setup. Verify a dedicated test control group
before activation. Group members have the existing shared control permissions;
this adaptation does not introduce a personal administrator allowlist.

```bash
sudo bash scripts/linux/install_release.sh activate \
  --root /opt/oopz --user oopz --release-id "$RELEASE_ID" --enable
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
  --root /opt/oopz --user oopz --artifact "$ARTIFACT" --sha256 "$SHA256"
sudo bash scripts/linux/rollback_release.sh \
  --root /opt/oopz --user oopz --release-id "$PREVIOUS_RELEASE_ID"
```

Before stopping anything, guards reject active tasks, corrupt/illegal PID locks,
symlink locks and unreadable state. Only a valid demonstrably dead PID is stale.
Do not use `--force` as a routine fix; it explicitly permits interrupting work and
requires operator review of the job and data-recovery consequences.

The switch journal captures actual current-link target, unit/logrotate file
bytes and metadata, and active/enabled service state. Any failure after stopping
must restore those actual snapshots, not a regenerated template. If restoration
fails, the service remains stopped and the journal is retained; never start an
uncertain release. After an abrupt process/host termination, inspect the journal
and recover using the reviewed management script:

```bash
sudo bash scripts/linux/install_release.sh recover --root /opt/oopz --user oopz
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
Chinese multipage PDF, model load/VAD/real audio, approved OOPZ capture and reconnect,
approved analyzer/Feishu flow, updates/rollback/failure recovery, reboot/enablement,
three shutdown phases and several hours of representative load. Initial target:
chunk close to completed transcription ≤240 s; report actual distributions,
queue growth, dropped chunks and peak resource use. Synthetic/mocked timings are
not capacity proof. Keep Windows regression results separate from Linux tests.
