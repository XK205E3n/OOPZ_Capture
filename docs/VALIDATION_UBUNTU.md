# Ubuntu adaptation validation record — 2026-10-01

> Historical record. The PDF reports, WeasyPrint backend and Node PDF tools described below were removed on 2026-10-04 when the flow became "record → automatic analysis → digest image"; the browser/PCM/speech/release results remain valid.

## 2026-10-02 explicit WeasyPrint backend

The new Linux backend is opt-in through `OOPZ_PDF_BACKEND=weasyprint`; existing
Chromium/Windows behavior stays the default, without automatic error fallback.
WeasyPrint 70.0 and Pydyf 0.12.1 executed locally with Pango 1.56.3. The project
declares and checks stable WeasyPrint 70.x, including the official EPS security
fix; earlier releases and unvalidated next-major APIs are rejected.

Final development regression: **425 passed, 9 skipped, 2 existing SDK cleanup
warnings**, 51.59 seconds. Python dependency checks, compilation, Node/shell syntax
and diff checks passed. The 9 skips remain 3 unavailable cloud-browser tests and
6 native Windows-only checks; target SDK browser tests were validated separately
on the preceding prepared Ubuntu release.

Real PDF tests verified Chinese text extraction, A4 multipage output, continuous
page counters, retained inline raster images and absence of both EmbeddedFiles and
FileAttachment annotations. Adversarial cases include network/local URLs, spoofed
raster MIME/EPS bytes, inline SVG and data-URI attachment channels. A first review
found attachment/SVG gaps; they were corrected with DOM filtering and independent
document-level attachment removal, then reproduced as absent in actual PDFs.
Source text/tails and ordinary links survive sanitization. Private resource URLs
and document contents are not written to diagnostics.

Resource limits cover the input, expanded HTML, Node heap and execution time, and
the isolated WeasyPrint address space plus outer process-group timeout. Failure and
interruption tests assert partial-output removal, no silent engine substitution,
and the minimum supported security/API version. Missing PDF helpers are rejected
before any installer dependency/service effects. No sample report or generated
private PDF is included in Git or release artifacts.

Representative synthetic PDFs and the separately authorized historical preview
were rendered and visually reviewed outside the source deliverable. These are
local conversion tests only: no analysis API requests or Feishu messages were
made. The new backend still requires exact-package installation and testing on the
Ubuntu target before its deployment can be marked complete. OOPZ capture/RTC and
its Chromium sandbox remain separate prerequisites.

Final synthetic visual/performance checks used the public project style and
fabricated report content: 11 pages in 1.754 s at about 207.79 MiB sampled process-
tree RSS, and 30 pages in 2.184 s at about 224.41 MiB. Sampling interval was 25 ms;
these are single development-host observations, not target-host guarantees or a
Chromium comparison. All 41 pages retained continuous counters, the complete
110-row table with repeated headers, CJK font mapping and ordinary links. Final
security filtering preserved the previously inspected page pixels. No private
report text, title, session identifier or source file is included here.

## 2026-10-02 deployment-preparation addendum

Re-fetched all remote refs. Latest Linux source was
`4e38d1dd50dd2fd90d10aff37341d7ee34082014`, including the queued-analysis guard;
main remained the Windows baseline. That exact source passed **388 tests, 9 skips,
2 existing SDK cleanup warnings** on the Debian development host. The subsequent
scoped preparation fixes passed **393 tests, 9 skips, 2 warnings** in 48.48 seconds.
All five added regressions ran, including the real PowerShell builder on isolated
clean Git fixtures and the real Linux dependency shell with external-tool doubles.
The 9 skips remain 3 unavailable local browser tests and 6 native Windows tests.

Official PowerShell 7.6.6 was materialized into ignored tooling and its tarball
checked against the vendor's published SHA-256. The original build reproducibly
failed its Unix venv and staging-prefix tests; the fixes retain Windows behavior,
include hidden files, and keep clean-source, manifest and hash checks. The Linux
preparation browser launch now explicitly selects the installed Chromium channel.
An executed runuser-reset simulation verifies that project HOME stays isolated.
This is not native Windows or privileged Ubuntu acceptance.

The pinned SenseVoice model's five required hashes were reverified. Real VAD found
speech in the same public 5.616-second sample and CPU ASR returned nonempty Chinese.
Observed VAD 0.93 s, model load 5.67 s, transcription 0.29 s, peak RSS 3,376,044 KiB.
These sequential one-sample results do not prove simultaneous browser/PDF/ASR
capacity on the target machine or a multi-hour latency guarantee. Dependency check,
Python compilation, Linux shell syntax, Node syntax and diff whitespace checks
passed. No new lint/type-check configuration is defined by the repository.

For this deployment the user explicitly approved an independent one-time substitute
release audit against the existing baseline (sensitive information, protected
paths, Git history and large files; redacted report retained outside Git). This
does not claim that the unavailable release-audit skill ran. The independent audit
and clean-HEAD formal build remain gates, not results implied by unit tests.

The user will provide configuration after program preparation. Until then, do not
activate the gateway or perform account-bound OOPZ, Feishu or analysis calls. The
target must independently pass browser/synthetic PCM, Chinese PDF, model/VAD/audio
and dependency checks. Live connections, actual recording/reconnect, reboot and
multi-hour workload remain unverified until those later authorized tests run.

### First target-host validation and concurrent test correction

The audited `f19cb4a60606` package completed preparation on Ubuntu 24.04. The target
ran all three actual SDK browser capture/PCM/probe tests successfully. Its full
suite reported 392 passed, 9 skipped, 1 failure: a prompt-format test assumed the
first concurrent request belonged to the first time window. A deterministic
event-controlled reproduction forced window two before window one and reproduced
that assertion failure while result status stayed completed and summary intervals
remained `[0, 300000]`, `[300000, 600000]`. The corrected test identifies requests
by their own evidence, covers both ordinary and forced-reverse execution, and
still asserts stage parameters, data minimization, costs, report content and
chronological output. It does not serialize or change production analysis.

Target PDF failed separately with Chromium's `No usable sandbox` diagnostic.
No disable-sandbox flag, AppArmor change, global sysctl change or production
activation was performed by this source fix. That host gate remains open pending
an approved sandbox-compatible remedy and an actual project-renderer retest.

Preparation now explicitly requests `chromium_sandbox=True` and tests either the
installed channel or the exact existing `MD_TO_PDF_CHROME_PATH` process override.
Both branches run through the real shell entrypoint with an isolated Playwright
double. This prevents the earlier preparation pass from hiding the host sandbox
requirement. Upstream SDK browser tests use Playwright's disabled-sandbox default;
those actual PCM tests are not sandbox acceptance, and this change does not vendor
or alter the SDK. The ordering regression passed 20 repeated runs (40 cases).
Final development-host regression for this correction: **395 passed, 9 skipped,
2 existing SDK cleanup warnings**, 49.22 seconds. Python compilation, shell/Node
syntax and whitespace checks also passed. Actual target sandbox/PDF testing remains
a separate required gate.

## Scope and provenance

Implementation branch: `codex/ubuntu-adaptation-implementation`, based on handoff
commit `5c264d4cb51ed4a7f1142a0fb582c2cf0614a273`. This record describes a development
candidate, not a production deployment or formal release. No production credentials,
server state, model-provider configuration or prompts were changed.

Actual execution host: Debian GNU/Linux 13 x86_64, Python 3.12.14, Node 24.19.0,
9 visible logical CPUs, about 9.7 GiB RAM, 32 GiB workspace. PID1 is the cloud task
runtime, not systemd. Visible resource counts are not dedicated-host guarantees.
Ubuntu 24.04 LTS and native Windows machines were not supplied for this task.

## Commands and actual results

- Created isolated `.venv`; installed project editable with speech/Feishu dependencies.
- CPU PyTorch `2.8.0+cpu` and torchaudio `2.8.0+cpu` installed from the official CPU
  index. ONNXRuntime 1.30.0, FunASR 1.4.1, ModelScope 1.39.1, Silero VAD 6.2.3 imported.
- `python -m pip check`: no broken requirements. Actual complete dependency freeze
  is preserved in the ignored validation directory; version ranges are not a full
  reproducible lock for every indirect Python dependency.
- Loaded Silero's ONNX VAD model and executed a 512-sample zero-input inference.
  This establishes model/runtime execution, not voice detection accuracy.
- Downloaded project-pinned SenseVoiceSmall revision
  `7bf452403abd7353a300cd760f7adae7701c92c1` and verified all five existing required
  SHA-256 values using `scripts/download_sensevoice_model.py`.
- Loaded the actual SenseVoice CPU backend and transcribed the model distribution's
  public `example/zh.mp3` (5.616 seconds; FFmpeg converted it to 16 kHz mono floats).
  Output was Chinese, nonempty: “开饭时间早上9点至下午5点。” Model load 6.644 s,
  transcription 0.499 s, process peak RSS 3,332,412 KiB. These are one short sample's
  observations; no multi-hour capacity or transcription-quality claim is made.
- Direct ASR on a separate one-second silence sample emitted text. Production
  silence handling relies on VAD; direct-ASR silence is not a correctness benchmark.
- Node package lock resolved and modules installed. The host's pnpm 11 reported
  ignored Puppeteer lifecycle builds; browser download was explicitly not used
  for this PDF smoke. Linux preparation uses the repository's pinned pnpm 10.11.0.
- Long Chinese PDF fixture reached installed Chromium launch with CJK fonts found.
  Launch failed with `socket: Operation not permitted` inside this cloud sandbox.
  No PDF was produced. No no-sandbox/security bypass was attempted.
- Python compileall, shell `bash -n`, JS `node --check`, and `git diff --check` passed.

Final integration: **364 passed, 9 skipped, 2 pre-existing SDK cleanup warnings**,
47.98 seconds. The 9 skips comprise 3 missing-Playwright-browser tests and 6 native Windows/PowerShell tests; skips are not acceptance evidence. Evidence commands:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/python -m pip check
python3 -m compileall -q src scripts/linux
for script in scripts/linux/*.sh; do bash -n "$script"; done
node --check tools/md_to_pdf.mjs
git diff --check
```

## Behavioral evidence, distinct from live acceptance

Linux integration tests invoke the real bash entrypoints and Python helpers,
including verified updater extraction → real installer → dependency-step contract.
Only external dependency installation, account/service actions are controlled
shims. Tests exercise helper absence, checksum/path rejection, shared setup writes,
failed-prepare retry, final-path venv behavior, actual link/file switch failures,
unit/logrotate/current snapshots, active/enabled/static restoration, readiness
failure, failed recovery, explicit journal recovery, and invalid/unreadable locks.
They do not assert source strings as a substitute for running the management code.

Runtime tests include shared-root link acceptance/internal-escape rejection,
malformed locks, and real isolated-child POSIX signals using controlled transport
and API doubles. They cover recording/transcription/API-wait stages, connection
cancellation, subprocess reaping and an in-flight send acknowledgement. Graceful
shutdown drains work; forced cgroup kill still has a remote-send uncertainty
window and is not an exactly-once delivery guarantee.

The SDK's missing-browser startup path can leave subprocess cleanup warnings
(`BaseSubprocessTransport`, event loop closed). These warnings existed before the
adaptation. They are preserved, not suppressed or claimed fixed in the vendored SDK.

## Acceptance matrix

| Requirement | Implemented/development evidence | Still requires authorized target host |
| --- | --- | --- |
| L01 | CPU dependencies, pip check, VAD/model loading, public audio sample | Ubuntu installation and representative real workload |
| L02 | Existing headless browser path retained | Matching Chromium launch, live join/PCM/reconnect |
| L03–04 | Node/path/cache/font checks and error/cleanup tests | Service-user multipage Chinese PDF; browser blocked here |
| L05–07 | Shared config persistence and link-boundary behavior tests | Migrated private configuration/state acceptance |
| L08–09 | Unit template, scoped stop/drain and signal tests | Real systemd boot/restart/cgroup cleanup; live three-phase stop |
| L10–12 / U1–U3 | Real-entrypoint isolated install/update/rollback/failure tests | Privileged fresh Ubuntu install, actual account ownership/systemd |
| L13 | Provider/summary contracts retained and mock regressions | Authorized test credentials/API/group, no production sends |
| L14 | Persistent logrotation template and no new ingress design | Host egress/RTC/reconnect/log-rotation soak |
| L15 | Migration/backup/rollback procedure documented | Actual old state/path reconciliation and no-duplicate verification |
| L16 | One public audio timing/RSS observation | Several hours, queue/dropped-chunk/swap/disk distributions |
| L17 | Cross-platform regressions and Unix-test collection guard | Native Windows/PowerShell regression and formal package build |

## Release gate

The `release-audit` skill referenced by AGENTS is not available in this execution
catalog or repository. The user explicitly authorized a one-time replacement audit
for committing and pushing this independent review branch. It checks sensitive
information, protected paths, Git history and large files against the existing
baseline; newly flagged findings require human review, and the baseline remains
unchanged. The redacted audit result is kept in ignored local logs. This does not
claim that the unavailable original skill ran or authorize a formal release.

PowerShell is unavailable here; `scripts/build_release.ps1` was updated but a formal
clean-HEAD release package has not been generated or validated. The original release
audit workflow remains required for a future formal release.

This branch is a review artifact. No merge, production deployment, live Feishu
message, or production data cleanup was performed.
