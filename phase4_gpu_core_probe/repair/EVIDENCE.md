# G3 bounded-core evidence history

The [README](README.md) owns the current contract, build procedure, test inventory
and remaining gates. This file owns execution results and the source identity
that each result tested. The raw `log/` evidence is Git-ignored; this tracked
summary does not replace its XML, binaries or hashes. Stages through §34 left
G3 adoption open. §35 records acceptance under the user-approved private-namespace
operating contract on the tested Thor host. Recorded PASS is not a new execution
or finalize.
Dates follow the design log's KST stage dates.

| Stage | Date | Patch prefix | Recorded GPU result |
|---|---|---|---|
| Initial bounded repair (§22) | 2026-10-26 | `351b8cd7` | 7 targets, 31 PASS |
| Lifetime review fixes (§23) | 2026-10-26 | `0fe547b3` | 15 targets, 73 PASS + 3 SKIP |
| Cleanup/drain (§24) | 2026-10-27 | `850d9d6a` | 34 targets, 102 PASS + 3 SKIP |
| Collision/closing/filter (§25) | 2026-10-27 | `76400ff8` | 38 targets, 109 PASS + 3 SKIP |
| Upstream test migration (§26) | 2026-10-27 | `76400ff8`, unchanged | 42 targets, 127 PASS + 3 SKIP |
| Lineage and documentation (§27) | 2026-10-27 | `76400ff8`, unchanged | 43 targets, 142 PASS + 3 SKIP, including 15 host-only lineage cases |
| Initialisation and request failures (§28) | 2026-10-27 | `76400ff8`, unchanged | 52 targets, 154 PASS + 3 SKIP, including 15 host-only lineage cases |
| Producer socket/context failures (§29) | 2026-10-28 | `76400ff8`, unchanged | Targeted 25 targets, 26 PASS; not a full probe |
| Producer review corrections (§29 follow-up) | 2026-10-28 | `76400ff8`, unchanged | Targeted 25 targets, 26 PASS; existing 13 initialisation filters preserved |
| FD receipt and reply recovery (§30) | 2026-10-29 | `24eb33e1` | 59 targets, 171 PASS + 3 multi-GPU SKIP, including 23 actual transport and 15 host lineage cases |
| FD receipt review corrections (§30 follow-up) | 2026-10-29 | `cc8185be` | 59 targets, 171 PASS + 3 multi-GPU SKIP; existing receive error/EOF and send-error cases now assert diagnostics |
| Post-receipt CUDA failure recovery (§31) | 2026-10-29 | `cc8185be`, unchanged | 59 targets, 174 PASS + 3 multi-GPU SKIP, including 26 actual transport and 15 host lineage cases |
| Post-import VMM setup failure recovery (§32) | 2026-10-30 | `cc8185be`, unchanged | 59 targets, 178 PASS + 3 multi-GPU SKIP, including 30 actual transport and 15 host lineage cases |
| Post-import review corrections (§32 follow-up) | 2026-10-30 | `cc8185be`, unchanged | 30 actual transport PASS and 13 host regression PASS; full 181-case probe and CPU verifier not rerun |
| Real imported-resource bind recovery (§33) | 2026-10-30 | `9a6a19d3` | 59 targets, 180 PASS + 3 multi-GPU SKIP, including 32 actual transport and 15 host lineage cases; separate 13 host regression PASS |
| Failure-path completion and owned-peer recovery (§34) | 2026-10-30 | `2e33677c` | Full device run passed; counts and unresolved resource-accumulation gate recorded below |
| Private namespace epoch containment (§35) | 2026-10-31 | `2e33677c`, unchanged | Selected namespace suite passed; G3 accepted under the private-epoch operating contract below |

## Initial bounded repair and primary-release comparison

Initial patch SHA256:
`351b8cd7d40bd5153d9763321c4a99fd0047f47cba7d79d245ed0455ee82f2f0`
(21 changed core files).

The primary-release injection first failed against
`fc52e218638ceb43cd01fd622200fd4e0969010192019068ae37cf501fa39a61`:
20 replacement pools succeeded and the failing release was called 21 times.
The `351b8cd7` repair rejected replacements and attempted release once. This
was an injected failure in the test executable, not an observed driver fault.
Evidence: `log/g3/repair-assessment.json`, including `primary_release_injection`.

The 31-pass run is a real historical result, but its quarantine test used a
mismatched stream. It did not establish failed-prepare quarantine; the old
busy-destructor defect made that path appear to pass. The later test injects an
actual ready-event recording failure, checks invalidation and poison, and
synchronizes before manager destruction. See the historical correction in
`log/g3/repair-review/assessment.json`.

## Lifetime review fixes

Patch SHA256:
`0fe547b39e369fbcf0a66f71c79e61e503f8f847bca195a86e9854e83aaed366`
(23 changed core files).

Ordinary busy destruction previously poisoned the process. Bound default/legacy
owners also checked the destruction thread before activating their retained
primary context. Five retirement and six cross-thread lifetime scenarios failed
against `351b8cd7` and passed on `0fe547b3`. These are corresponding scenarios,
not byte-identical test sources: GREEN added public `poisoned()` assertions.

At that stage, six upstream test sources were reused. Two metadata-cache cases
first failed because of a hardcoded legacy namespace; attachment was changed to
`shm_name_for_block()`. Manager, mapper, BufferRef and VMM migration, plus the
strict-binding special-stream oracle, were still pending **at that stage**.
The later upstream migration completed that work; multi-GPU execution remains
limited as described in the current README.

Results were custom 43 PASS plus upstream 30 PASS/3 multi-GPU SKIP. CPU was not
rerun in this stage; 466 PASS/118 opt-in SKIP referred to the earlier verifier.
Evidence: `log/g3/repair-review/{assessment.json,previous/,verification/}`.
Nine RED-run sockets were removed in a limited cleanup recorded for this stage.

## Cleanup/drain

Patch SHA256:
`850d9d6ad38ce1de8685dcfdbb1508ddc3a15c54c1e1ace1876f86cd749b4c32`.

Ten cases failed against `0fe547b3` and passed after repair: three VMM rollback
faults, two final producer context restoration faults, failed-bind event cleanup,
and four subscriber context-pop ordering/failure cases. The old mapper had no
close API, so RED observed the same charged-read/reference completion condition
directly; GREEN used public `try_close()`. Producer RED suppressed the old orphan
scan to preserve unrelated names.

Synthetic read-completion fixtures exercised real CUDA events and references
without dereferencing their pointer. Seven separate actual IPC import cases
covered Unmap/AddressFree/Release/imported EventDestroy, successful import context
pop and partial-import Unmap/pop failures. They checked initial cleanup calls
and no new imports/retains through 20 mapper recreations.

The pending-producer case queried a stream containing the ready wait before a
consumer gate/kernel. Consumer lifetime cases warmed kernel initialization before
a bounded host callback gate and reset the checksum sample after warmup.

Results were custom 72 PASS plus upstream 30 PASS/3 multi-GPU SKIP. The CPU verifier
was rerun: 12 packages, 39 targets, 584 collected = 466 PASS + 118 opt-in SKIP.
Evidence: `log/g3/cleanup-drain/{assessment.json,verification/,device-session.json}`.

## Collision, closing visibility and empty filters

Patch SHA256:
`76400ff87477f1048ac1a4adb41229b14c34c4ddb2b76865c55376bcbf5845c0`.

Two name-collision and two empty-filter cases failed against `850d9d6a` and
passed after repair. Three additional mapper-state cases were new GREEN cases,
not part of those four RED cases. Results were custom 79 PASS plus upstream
30 PASS/3 multi-GPU SKIP. The CPU verifier again passed 466 cases with 118 skips.

Collision RED disabled the metadata logger because ERROR output could alter
`errno` and hide the old EEXIST deletion branch. The old inode lost its link and
the published name referred to a replacement. Final collision XML and
`red/collision.log` are authoritative; `red/ctest.log` retains the earlier masked
collision PASS and empty-filter RED. Do not treat every saved RED log as the
same execution. The corrected test reopens the name and checks inode, link count
and full bytes. Actual transport drain also passed.

Evidence: `log/g3/review-fixes/{assessment.json,red/,verification/,device-session.json}`.

## Upstream test migration

The production patch remained `76400ff8` above. The existing repaired static core
was reused, SHA256
`ae3c9ddd5b508bc551d70d8eb2fab6ccf4339dcff99e942393a75924f31a562b`.

Five migrated targets contained 22 cases: 20 PASS + 2 multi-GPU SKIP. Five unchanged
targets added 28 PASS + 1 multi-GPU SKIP. With the custom 79 cases, the whole run
had 42 targets and 130 collected = 127 PASS + 3 SKIP. Direct migrated GTest,
whole-probe CTest and the CPU verifier were separately executed; CPU again
reported 466 PASS + 118 opt-in SKIP.

The first VMM fixture recorded ready after leaving the primary context and failed
strict default-stream binding. Moving recording inside that context passed. This
was a fixture migration failure, not a new production-core RED/GREEN result.
Read-handle fixtures use synthetic pointers. VMM's complete 4,096-byte CPU readback
is an import/data oracle, not copy-free evidence or a mapper H-bound proof.

Evidence: `log/g3/upstream-migration/{assessment.json,reconstruction.json,device-session.json,verification/}`.

## Lineage and documentation

The production patch and static core stayed unchanged. A tracked lineage lock
classified all 13 upstream core test files, recording the base revision, original
hashes, six adapted paths and the separate helper. Preparation and CMake configure
now validate that relationship. CMake uses the lock as its executable source registry.
The prior archive/whole-tree pin verification remains in place.

Fourteen contract tests failed before implementation and passed afterward. A
further negative test demonstrated that classifying a test source as support
could remove its registration; validation was tightened and all 15 cases passed.
These are host validation development results, not new GPU-core defect evidence.

Actual build comparisons preserved the source, compile/link flags and linked
dependencies of 11 native executables, and the commands, timeouts and XML paths
of all 42 existing CTest entries. Only the lineage pytest entry was added.
The final probe had 145 cases = 142 PASS + 3 multi-GPU SKIP across 43 targets.
The CPU verifier again reported 466 PASS + 118 device opt-in SKIP. Direct migrated
GTests and the complete transport suite passed. Temporary module cleanup preserved
the existing module and 13 sockets, with no new metadata/socket names remaining.

README history was separated from its current contract. G4 runtime ownership and
GpuTensor's existing wire invariants were documented; message field declarations
and order were preserved. Runtime/adapter implementation, broad harness extraction
and moving the experiment peer were not part of this stage.

Evidence: `log/g3/lineage-integration/`, including `assessment.json`,
`build-contract-comparison.json`, RED/GREEN XML, `device-session.json` and `verification/`.

## Initialisation and request failures

The production patch and repaired static core remained unchanged. The upstream
MIT permission notice was copied verbatim beside the six adapted test sources.
Manager coverage now checks GPU-complete, reader-free rejection at 0/99,999 us,
same-block reuse at 100,000 us, a fresh UID and publication after cancellation.

Seven isolated producer cases fail the second block's address reserve, allocation,
map, access, FD export, event creation or IPC event handle creation. They check a
single injected failure, allocation/release call balance, actual recorded names,
stable FD counts, and both replacement-manager and same-manager restart.
Subscriber event-create and ready-wait failures preserve publication ownership
for a successful retry, then return the reference and H=1 permit on drain.
The latter fixture uses a synthetic pointer and establishes no payload coverage.

Two real Agnocast transport cases inject receive error/EOF after successful Unix
connect. Three rejected requests preserve FD/metadata-map counts; the fourth
imports the original descriptor, verifies the GPU checksum, and closes normally.
Failure occurs before SCM_RIGHTS receipt, so this is not malformed ancillary-data
or already-received FD cleanup coverage. These are coverage additions against
the existing repaired core, not a new production-defect RED/GREEN comparison.

Results: 52 targets, 157 collected = 154 PASS + 3 multi-GPU SKIP; custom 90 PASS,
upstream 49 PASS/3 SKIP and host lineage 15 PASS. CPU verification again passed
466 cases with 118 device opt-in skips. Manager/BufferRef and the new isolated
initialisation/bind filters also ran directly. After the full GPU run, socket
creation-count assertions were added and the affected producer/manager targets
were rebuilt and rerun. Current XML and direct-run evidence include those assertions.
The matching temporary module was unloaded at refcount zero; the existing module,
13 sockets and original metadata inventory were preserved.

Evidence: `log/g3/initialisation-faults/{assessment.json,reconstruction.json,
device-session.json,verification/}`. Full G3 adoption, complete diagnostics and
production recovery remain open. No finalize or Git mutation ran in this stage.

## Producer socket and initial context failures

The canonical core patch and repaired static archive remained unchanged. Six
isolated cases were added: second-block socket/bind/listen, second-event context
push, initial primary-context retain and allocation-context push. The retain
interposer previously observed calls only; it now returns the selected failure.
Bind/listen injection is restricted to pool socket paths; socket injection targets
AF_UNIX stream creation. The bind error is injected without a competing socket
object, and the socket error does not exercise the CLOEXEC fallback.

All initialisation cases check rollback without poison, recorded metadata/socket
removal, FD counts and both replacement-manager and same-manager restart. The
second-block cases balance successful CUDA allocation/release calls, now also
including context push/pop and primary retain/release. Early-context failures
occur before VMM allocation; retain failure creates no metadata either. Calling
thread top-of-stack context identity is checked after rollback and successful retry;
the push/pop counts, rather than identity alone, detect same-primary-context push leaks.
These are coverage additions, not a new production-core defect RED/GREEN result.

The pre-edit seven-case run was observed in the session but its log was not
separately frozen in this evidence directory. The preserved final verification
passed **26 cases across 25 CTest targets**: 13 initialisation, 11 existing terminal
cleanup and two isolated-runner checks. The six new filters also passed directly.
CMake collected 58 targets, but the remaining probe targets and actual transport
were not rerun. This result must not be added to the earlier 154 PASS and presented
as a single full-probe execution. No kernel module was loaded or unloaded.

The CPU verifier was separately rerun: 12 packages, 39 targets, 584 collected =
466 PASS + 118 device opt-in SKIP, zero failures/errors. The context-identity
assertion was added after the first targeted run; the final build, CTest and direct
XML contain that assertion. Evidence: `log/g3/initialisation-system-faults/`, including
`assessment.json`, `reconstruction.json`, build logs and frozen test XML/source hashes.

Direct-run logs identify socket/bind/listen and primary-retain/allocation-context
errors. The second-event context-push error is still reported at the event-creation
boundary. Current diagnostic, FD and recovery gaps are maintained in the README.
Finalize, commit-plan and Git mutations were not run.

## Producer initialisation review corrections

Review `code-review-20261028-093809.md` identified renewed execution-history
duplication in README and six new LOW items. README now describes contracts and
registered coverage; execution totals and stage history live here. The stage
index includes §29. The unpreserved pre-edit baseline is explicitly distinguished
from frozen evidence, and current FD/parser gaps are maintained in README.

The primary-release wrapper now observes calls without recording fictitious
injections. Context cases use their own explicit occurrence/metadata/release
parameters. A shared fixture owns warm-up, baseline, rollback and restart checks;
both suites acquire two distinct replacement blocks. The second-event context
case also confirms one IPC event handle existed before failure. Header/comments
state the process-wide socket selection and the separate roles of top-of-stack
identity and push/pop balance.

Rebuilt producer executables passed **26 cases across 25 CTest targets**; all 13
initialisation filters also passed directly, each selecting exactly one case.
All 58 CTest registrations remained identical. A standalone driver-call probe
selected PrimaryRelease against the previous frozen and current interposers:
both forwarded one successful release, but the former reported one injection
and failed the observer oracle; the latter reported zero and passed. This is a
harness correction, not a GPU-core defect result or an additional registered case.

README's inventory totals 96 custom, 52 upstream and 15 lineage cases; it contains
no PASS/SKIP execution totals. The canonical patch/static core are unchanged.
CPU verification, the full probe, transport, kernel module operations, finalize
and Git mutation were not rerun for these review corrections. Earlier CPU results
remain in `log/g3/initialisation-system-faults/`; current evidence is separately
preserved in `log/g3/initialisation-review-fixes/`, including `assessment.json`,
registration comparison, direct/CTest XML and the before/after observer probe.

## FD receipt and reply recovery

The six-case real Unix-socket reproduction against patch `76400ff8` recorded
two passes and four failures. Two FDs, two sender-side SCM_RIGHTS headers, and
eight FDs truncated to the receiver buffer each leaked an installed FD. A
separate capacity-limited receive installed exactly one FD with `MSG_CTRUNC`:
it did not leak, but incorrectly proceeded to CUDA primary-context retention.
The reproduction's source, static archive, executable and XML remain in
`log/g3/fd-receive/red/`; the two-header sender may be coalesced by Linux.

The canonical patch now walks every returned SCM_RIGHTS FD, rejects anything
other than one FD without control truncation, and closes all installed FDs on
rejection. Patch SHA256 is
`24eb33e1c1a41433ddd5a347b21f77cb754675bc05403e6398d056a59265abd1`.
Only the importer source changed relative to the preceding reconstructed core;
all 23 patched files reconstruct with zero fuzz and verified bytes. The core
and every linked probe target were rebuilt. The original six cases passed;
two added context-push/import failure cases bring the native receipt group to
eight. These use `/dev/null`, deliberate CUDA failures, FD-number/CLOEXEC/EBADF
and count oracles, refcount/H=1 retries, primary-release and push/pop counts,
context identity and non-poison checks. They do not simulate successful import.

Three new actual Agnocast process cases send two/eight copies of the producer's
VMM FD, or inject `EPIPE` before `sendmsg`, for the first three requests. The
malformed responses use real `sendmsg`; the test asserts each one-byte send
succeeded, so an unrelated send failure cannot masquerade as parser rejection.
Both peers' settled FD/metadata-map counts return to baseline. The fourth request
uses the same descriptor, original VMM FD and H=1 mapper, imports successfully,
checks the GPU checksum and drains normally. The injection selects only a
single-FD SCM_RIGHTS reply from the core's Unix-socket path and preserves the
sender's FD and `MSG_NOSIGNAL` flags. No producer implementation change was needed.

After tightening successful-pop and malformed-send observations, the final full
probe passed **171 cases with three multi-GPU skips across 59 CTest targets**:
107 custom, 49 upstream and 15 host lineage passes. GTest's `result="skipped"`
attribute is included in this classification. Actual transport passed all 23
cases, including the 1,000-delivery checkpoint and crash/restart cases. The final
eight native receipt cases and one VMM child-process test also passed directly;
these overlap the full run. CPU `scripts/verify` passed 12 packages/39 targets,
584 collected = 466 PASS + 118 device opt-in SKIP, with no failures/errors.

Before device execution, three ARM64 artifacts, 24 source/prior-artifact hashes,
ELF machine, kernel/vermagic and module srcversion matched the recorded identity.
The temporary module was unloaded at refcount zero after each full run; the
pre-existing module, 13 sockets and metadata inventory were preserved.
`log/g3/fd-receive/assessment.json`, `device-session.json`, `reconstruction.json`,
`green/` and `verification/` preserve results and source/binary/evidence hashes.

Full G3 adoption remains open. Successful GPU retry after each post-receipt CUDA
failure, remaining CUDA/system-call combinations, operation-specific diagnostics
and production recovery remain separate work. G4/G5 and Orin are unfinished.
Finalize and Git mutation were not run for this change.

## FD receipt review corrections

Review `code-review-20261029-043150.md` reported three new LOW items. EOF had
used the errno left by an earlier call, `ScopedFd` implicitly allowed copying,
and the reply-injection header omitted its three-call limit and counter meanings.
The importer now diagnoses `recvmsg == 0` without errno, preserves the negative
return's error diagnostic, deletes both copy operations, and documents ownership
of the returned FD. The header states the selectors, first-three-reply limit,
pass-through-inclusive attempts and successful malformed-send count.

The existing receive-error/EOF and producer send-error tests now inspect the
subscriber log after normal peer exit. Before the core change, receive-error
passed while both EOF cases failed the new diagnostic assertions. With the
updated core, the two EOF cases each report three explicit EOF messages without
the previous errno diagnostic; the receive-error case retains its error messages.
Compile-only probes include the actual importer: copy construction and assignment
compiled before the change and are rejected specifically as deleted afterward;
ordinary construction/reset still compiles. These probes add no registered cases.

The new canonical patch SHA256 is
`cc8185bed6cda674be7db84623dafe6d87867e4ed3a7e243dc1477bf1df0bf89`.
Only the importer changed relative to the preceding repaired tree. Reconstruction
again verified every byte with zero fuzz; the installed core archive and all
linked probe targets were rebuilt after the final patch edit.

The final full run passed **171 cases with three multi-GPU skips across 59 CTest
targets**, including all 23 actual transport cases. The eight native FD cases
also passed directly and overlap that total. CPU `scripts/verify` passed
12 packages/39 targets, 584 collected = 466 PASS + 118 device opt-in SKIP,
without failures or errors. RED and GREEN device runs verified the ARM64
artifacts/source/kernel identity, unloaded the temporary module at refcount zero,
and preserved the existing module, 13 sockets and metadata inventory.

Evidence is separate from the original §30 run:
`log/g3/fd-receive-review-fixes/{assessment.json,reconstruction.json,
core-identity.json,ownership-probe.json,red/,green/,verification/}`.
The cumulative LOW/refactoring/design suggestions were not expanded into this
fix. Full G3, the documented post-receipt CUDA-success retry gap and G4/G5 remain
open. Finalize and Git mutation were not run.

## Post-receipt CUDA failure recovery (2026-10-29)

Three actual Agnocast peer cases now receive a valid producer VMM FD before
injecting primary-context retain, first context push, or VMM import failure.
Each fails the first three maps, then imports the same cached UID through the
same H=1 mapper, verifies the GPU checksum and drains normally. Startup calls
cannot consume these failures: the map-thread observation arms only after a
well-formed, single-FD SCM_RIGHTS receipt from the producer socket.

Every rejection verifies CLOEXEC and immediate EBADF, context restoration,
zero read references, nonpoison, and settled FD/metadata-map counts on both peers.
Retain failure makes no push/import/release calls. Push failure releases its one
successful primary retain. Import failure returns that retain and balances two
successful push/pop pairs, including the partial-import cleanup guard. The fourth
map performs a real import with three successful push/pop pairs. After mapper
drain, all-thread successful retain/release counts and push/pop counts balance;
worker polling counts are variable and are not asserted as an exact total.

The initial targeted run passed all three new cases. Read-only inspection found
that a successful import can close its received FD before later driver work
reuses the same number. The final test therefore requires EBADF on synchronous
rejection only and uses settled FD counts for successful import. After that
assertion adjustment and a rebuild, the full probe passed **174 cases with three
multi-GPU skips across 59 CTest targets**: 110 custom, 49 upstream and 15 host
lineage passes. Actual transport passed all 26 cases, including the 1,000-delivery
checkpoint and crash/restart scenarios. The targeted three cases overlap this
total. CPU `scripts/verify` passed 12 packages/39 targets, **584 collected =
466 PASS + 118 device opt-in SKIP**, without failures or errors.

The core patch remains `cc8185bed6cda674be7db84623dafe6d87867e4ed3a7e243dc1477bf1df0bf89`.
No core modification was needed. All 140 reconstructed source hashes and both
built/installed core archive hashes match the previous verified reconstruction;
this run rebuilt the probe, not the core or a new empty build tree.

Device runs verified all five recorded artifacts and 24 source/prior-artifact
hashes, ELF architecture, kernel/vermagic and module srcversion. Each temporary
module was unloaded after peers exited and its refcount reached zero. The
pre-existing module, 13 sockets and metadata inventory were preserved.
Evidence is in `log/g3/post-receipt-recovery/`, including `assessment.json`,
`targeted/`, the initial `full/`, the final `final/`, and frozen `verification/`.

These results close the three post-receipt CUDA-success retry cases. Remaining
failure combinations, operation-specific diagnostics and production recovery
still keep G3 open; G4/G5 and Orin remain unfinished. Finalize and Git mutation
were not run for this change.

## Post-import VMM setup failure recovery (2026-10-30)

Four actual Agnocast peer cases extend the receipt fixture to allocation-granularity
lookup, address reservation, mapping and access setup failures after a successful
VMM import. Each injects the selected failure for three maps, then imports the
same cached UID through the same H=1 mapper, verifies the GPU checksum and drains.
No core change was required.

Every failed map proves one successful import and the expected stage-attempt counts.
Granularity/reserve failures successfully release the imported allocation; map
failure also frees the reserved address; access failure also unmaps it. No failed
attempt opens an IPC event. The VMM return counters count successful CUDA calls,
not merely attempted cleanup. Each rejection restores the caller context, balances
two successful push/pop pairs and one primary retain/release, returns the read
reference/admission without poison, and preserves both peers' settled FD/map counts.
The selected operation produces exactly three matching diagnostics.

The received FD's first successful `close()` is now observed on the map thread in
all seven receipt cases. The new post-import failures permit the driver to reuse
that FD number before map returns, so they do not require map-return EBADF. The
earlier retain/push/import failures retain their EBADF checks. The fourth successful
map reaches all VMM stages and event open without synchronous VMM cleanup; after
checksum/drain, all-thread context calls balance. The VMM counters cover synchronous
rollback, not physical VRAM/event/VMM residency or a payload copy trace.

The targeted four cases passed. The full probe passed **178 cases with three
multi-GPU skips across 59 CTest targets**: 114 custom, 49 upstream and 15 host
lineage passes. All **30 actual transport cases** passed, including existing
receipt/cleanup failures, 1,000 deliveries and crash/restart. The targeted four
overlap the full result. GTest skips are recorded as `result="skipped"` in XML.
CPU `scripts/verify` also passed 12 packages/39 targets: **584 collected =
466 PASS + 118 device opt-in SKIP**, without failures or errors.

The canonical patch remains `cc8185bed6cda674be7db84623dafe6d87867e4ed3a7e243dc1477bf1df0bf89`.
All 140 reconstructed source hashes and both built/installed core archive hashes
match the prior verified reconstruction. Only the probe was rebuilt; no fresh
core reconstruction/build or empty build-directory run is claimed.

Both device sessions verified five artifacts and 24 source/prior-artifact hashes,
ELF architecture, kernel/vermagic and module srcversion. Each temporary module was
unloaded at refcount zero; the existing module, 13 sockets and metadata inventory
were preserved. Evidence is under `log/g3/post-import-recovery/`: build logs,
`core-identity.json`, `targeted/`, `full/`, `assessment.json` and frozen `verification/`.
The targeted run retains per-peer logs; full-suite results are frozen XML/CTest logs.

This closes four further recoverable post-import failure paths. Remaining
allocation/event/syscall/thread/asynchronous-CUDA combinations, diagnostic audit
and production supervisor/recovery keep G3 open. G4/G5 and Orin remain unfinished.
Finalize and Git mutation were not run for this change.

## Post-import review corrections (2026-10-30)

Review `code-review-20261030-063055.md` identified a teardown timing defect in the
untracked device-session runner: peer exit does not imply all inherited device
holders have closed. A new runner under `log/g3/post-import-review-fixes/` waits
up to five seconds for refcount zero, records the initial/final counts and elapsed
wait, and only then attempts one normal unload. It never force-unloads or kills
the unlink daemon. Teardown failure has exit 70 and preserves the test exit code,
JSON and summary, including nonzero/failed-to-launch `rmmod`. Existing stages are
rejected without overwriting evidence. Previous runners and frozen records remain
unchanged; promotion of a canonical tracked runner remains a separate task.

Initial host regressions reproduced six failures and one pass with the old runner.
The corrected runner passed all seven cases with mocked module/subprocess/clock
state, including delayed close, persistent holders, test failure, unload failure
and stage reuse. Final checks also assert the JSON summary on unload failure.
Six host protocol checks passed after an initial six-failure run: named fields,
exact token count/tag, complete pre-import VMM expectations and the colon-qualified
context diagnostic. These host regression files are scenario evidence, not new
CTest registrations.

All seven receipt modes now compare every VMM field, filling unspecified expected
fields with zero. Retain/push/import must show only one received-FD close and zero
VMM-stage/cleanup counts. EBADF still depends on the expected lack of a successful
import, independently of table completeness. Each mode requires its full diagnostic
prefix three times and the other six prefixes zero times. Positive matching is
independent of the negative-check loop. Named Python field consumption validates
the existing C++ protocol without changing it. Balanced drain push/pop counts
retain their lower-bound check because worker polling can add pairs.

The revised real transport suite passed **30/30**, including the seven receipt
cases, in 34.14 seconds. Native sources and all 29 binary hashes still match the
preceding verified run; neither core nor probe was rebuilt. The full 181-case probe
and CPU verifier were not rerun. Their latest full results remain the preceding
stage's 178 PASS/3 multi-GPU SKIP and 466 PASS/118 device opt-in SKIP, respectively.
The history table now includes both previously omitted §31 and §32 runs.

The new session observed refcount **2 immediately after tests, then 0 after
0.201 seconds**. `rmmod` and the runner exited zero. All five artifacts and 24
source/prior-artifact identities were verified, and the existing module, 13 CUDA
sockets and repaired-core metadata inventory were preserved.
`cuda.shm.*` was separately observed at **911 → 945 files**, **74,629,120 →
77,414,400 bytes** (+34 files/+2,785,280 bytes). These driver objects are outside
`metadata_preserved`; none were removed. Safe deletion and PID/name reuse behavior
remain unresolved, and the counts do not establish physical GPU resource residency.

Evidence: `log/g3/post-import-review-fixes/{run-device.py,test_runner.py,test_stats.py,
runner-red.*,runner-green.*,stats-red.*,stats-green.*,transport/,identity.json,
assessment.json,verification/}`. The transport run retains all peer logs and is
the formal evidence for these changes; scratchpad review reruns were not promoted.
G3 remains open. Finalize and Git mutation were not run.

## Real imported-resource bind recovery (2026-10-30)

The existing synthetic-publication bind tests did not exercise actual VMM/IPC
resource rollback. Two new receipt modes fail consumer completion-event creation
or ready-event wait three times after importing a real producer allocation and
ready event, then map the same cached UID with the same H=1 mapper, check the GPU
checksum and drain. IPC event-open failure/retry was already covered by the
existing `bad_event` transport case and is not counted as new coverage.

The old core passed the new resource-return, successful-retry and drain assertions
but failed both operation-specific diagnostic assertions (zero instead of three).
The canonical patch adds only the two missing CUDA-operation diagnostics to
`read_handle.cpp`, preserving guard restoration and return behavior. The new
patch SHA256 is
`9a6a19d3060c6f5d42dc4585a1fdbaa923f3563172b9e9bca4eeb67c6ef2f1df`.
Zero-fuzz reconstruction verified all 140 sources; only that core file differs
from the preserved previous reconstruction. Core and probe were rebuilt afterward.

The final full run passed **180 cases with three multi-GPU skips across 59 CTest
targets**: 116 custom, 49 upstream and 15 host lineage PASS, including **32 actual
transport PASS**. GTest's `result="skipped"` attributes are included in this count.
The CPU `scripts/verify` gate passed **466 cases with 118 device opt-in skips**
across 39 targets and 12 built packages. The unchanged runner/protocol host
regressions also passed **13/13**.

The full CTest XML and logs were preserved. Its temporary peer logs were removed
by later CPU pytest cleanup, so the unchanged transport suite was rerun with
`--basetemp=log/g3/bind-recovery/transport/peers` to retain each peer's stdout and
stderr. That separate **32/32 PASS** overlaps the full run and is not added to it.
The nine receipt cases check complete VMM and bind counters, selected diagnostic
three times, other operation diagnostics absent, ref/context return and nonpoison.

All three device sessions used the corrected existing runner with an absolute
stage argument, verified five artifacts and 24 source/prior-artifact identities,
and preserved the pre-existing module, 13 sockets and repaired metadata names.
The full session observed refcount zero immediately; the final transport session
observed **2 → 0 after 0.100 seconds**. Each unloaded normally. `cuda.shm.*` counts
were **945 → 947** (RED), **947 → 1013** (full), and **1013 → 1049** (transport);
bytes were **77,414,400 → 77,578,240 → 82,984,960 → 85,934,080**. None were deleted.
These counts are execution observations, not GPU residency or copy measurements.

Evidence: `log/g3/bind-recovery/{assessment.json,reconstruction.json,red/,full/,
transport/,host-results.xml,cpu-verify.log,verification/}`. The runner remains
under `log/g3/post-import-review-fixes/`; tracked promotion D5 is still separate.
Remaining producer granularity/metadata, FD fallback/thread, asynchronous CUDA,
operation-diagnostic and production recovery work is listed in the README matrix.
G3 remains open; G4/G5 and Orin remain unfinished. Finalize and Git mutation were
not run for this change.


## Failure-path completion and owned-peer recovery (2026-10-30, §34)

The full current-revision device run passed. G3 adoption remains open because the
resource-accumulation question below is unresolved. The canonical patch is
`2e33677cd3f71ed718ab40ec491a32dabf987c79f0eb3426964c3777f892e715`;
zero-fuzz reconstruction verified all 140 source files, including 23 patched core
files. The table separates the full run from its included subsets; subset passes
are not added again to the total.

| Check | Recorded result |
|---|---|
| Full Thor/device-and-host probe | 81 targets, 264 collected = **261 PASS + 3 multi-GPU SKIP**, failures/errors zero |
| Actual transport subset | **37 PASS**, included above |
| Supervisor device subset | **4 PASS**, included above |
| Session/protocol host subset | **25 PASS**: 15 runner + 10 protocol, included above |
| Recovery host subset | **25 PASS**, included above |
| CPU verifier | 39 targets, 584 collected = **466 PASS + 118 device opt-in SKIP**, failures/errors zero |
| G3 adoption | Open, including the resource-accumulation question below |

An earlier 79-target native invocation also completed successfully with actual
transport/recovery opt-ins disabled. The full run above supersedes that invocation
for the tested source; the earlier skips are not device coverage. Two review
findings were corrected and rechecked; the recheck reported no remaining findings.
That review result does not close the resource-accumulation gate.

The initial full-device run in this stage contained 261 cases: 258 PASS and three
multi-GPU skips, including 36 transport and two supervisor device passes. The
latest `gate-device` rerun above adds one actual mapping-access observation and
two terminal-poison recovery cases. An independent review reported no findings.
Both full runs used the same canonical core patch; their totals are not added.

Producer tests now cover initial granularity lookup, first/second owned metadata
`ftruncate`/`mmap`, exported/listener close-on-exec setup, server thread creation,
allocation exceptions and real socket-name collision. The core fails closed when
fcntl fails, rolls initialisation exceptions back before rethrowing, and removes
only socket names it successfully bound. The first producer fixture run exposed
incorrect oracle expectations; those were corrected before subsequent native
verification rather than being counted as core rollback regressions.

Subscriber additions cover completion-create/ready-wait context activation,
queue construction/thread failures and worker allocation behavior. The previous
worker's temporary deque allocation in a `noexcept` thread was reproduced as
SIGABRT (return code -6) under the targeted allocation failure. The repaired
worker processes and waits without that allocation. Failed queue setup retains
the charged read and terminally rejects further admission. Existing asynchronous
record/query/cleanup tests remain distinct from these additions.

Native request tests add CLOEXEC fallback and request-socket/fcntl rejection and
retry. Actual peer scenarios add accepted-FD F_GETFD/F_SETFD and accept EINTR/EIO
errors followed by same-descriptor import, checksum and drain. Accept injection
first closes a real accepted FD, then returns the chosen errno; it exercises the
server's error branch rather than real kernel exhaustion. The existing bad-event
case now requires 51 event-open diagnostics but still does not assert the full
receipt VMM rollback counter set. CUDA fault injection skips the selected driver
call, so observed cleanup and retry do not describe all real-driver partial states.

The added mapping-access case queries `cuMemGetAccess` on the consumer's actual
imported address and requires `CU_MEM_ACCESS_FLAGS_PROT_READWRITE` (3), then
checks the payload and drains. It records native GPU permissions explicitly;
CPU heap read-only protection does not imply a read-only GPU mapping.

The diagnostic audit (D6) combines producer/bind context and event errors, parser
rejection details, descriptor rejection reasons and mmap errno preservation in
this patch revision. Synthetic bind fixtures preserve their publication; actual
mapper bind failure drops it and a retry repeats the full VMM/event import. The
latter has an unmeasured cost and is not an import-cache performance claim.

The tracked `session/` runner and host regressions implement D5. The runner rejects
relative stages and empty commands, preserves existing stages, validates the
existing ARM64 artifact/source manifests, waits for refcount zero and records
test and teardown results independently. A session-specific TMPDIR plus a separate
G3_PEER_LOG_ROOT preserve peer stdout/stderr across later pytest cleanup. Original
runner copies and frozen evidence remain under `log/g3/post-import-review-fixes/`.
The source/artifact manifests still depend on the existing nested fork and device
build; tracked tooling alone does not make an outer clean checkout reproducible.

The owned-peer supervisor registers a complete directly launched group before
GPU traffic, pins each resource inode and child pidfd, and waits for every peer
to die before removing registered unchanged names and spending a bounded restart
budget. It includes unpublished resources. Missing startup registration, foreign
ownership or replaced names prevent automatic cleanup/restart; an old stage is
not adopted after supervisor failure. These are fail-closed limits, not recovered
cases. The latest full run's supervisor device cases cover unpublished-resource
normal stop, three reader-SIGKILL restart cycles, and two terminal-poison paths
(`partial_unmap` and `event_destroy`). For poison, the fixture requests peer quit,
observes exit code 1, invokes `Group.restart()`, and verifies a fresh checksum
while an independent pair continues to progress. This verifies exit-driven group
recovery; it does not reset or repair a live poisoned mapper.

During an early host runner argument test, the subprocess guard was missing and
the test accidentally loaded the verified test module once. No intended test
command ran; normal unload completed with rmmod exit zero. The recorded existing
module, sockets and metadata were preserved, and the driver shared-memory count
and bytes were unchanged. The session record is preserved at
`log/g3/completion/host-test-isolation-incident/`. Host fixtures were changed to
reject unexpected subprocesses by default. This incident is not a host-only test
result and is not omitted from the stage history.

The initial full-device runner observed module refcount **2 → 0 after 0.05022 seconds**,
then normal rmmod and runner exit zero. Existing sockets, module and repaired
metadata were preserved. `cuda.shm.*` changed from **1,116 files / 91,422,720 bytes**
to **1,205 files / 98,713,600 bytes**: +89 files / +7,290,880 bytes.

The standalone [event probe](residency/event_probe.py) subsequently narrowed the
reproducer. It calls `libcuda`/`libcudart` through ctypes without Agnocast, heaphook
or the repaired core. CUDA calls in the completed sequences returned zero,
including explicit event destruction, but runtime IPC-event cycles left a named
object after process exit. The earlier mixed-event run increased the inventory
from **1,207 to 1,209 files**, one new object in each of its last two mixed cycles.
The more specific runtime-event run increased it from **1,209 to 1,213 files**:

| Event-probe modes, in execution order | New objects per cycle |
|---|---|
| `event`, `export`, `pair`, `crash_pair` (driver only) | 0, 0, 0, 0 |
| `runtime_init`, `runtime`, `runtime_callback`, `runtime_exit` | 0, 0, 0, 0 |
| `runtime_event`, `runtime_event` | 1, 1 |
| `mixed_event`, `mixed_event` | 1, 1 |

Each new object was **81,920 bytes**. Runtime-only IPC-event
create/record/synchronize/export/destroy reproduced the residue, so it is not
specific to mixed runtime/driver calls. The [VMM probe](residency/vmm_probe.cpp)
observed no new objects in driver-only allocate/export and normal/crash IPC
import sequences. These finite observations isolate a sufficient reproducer;
they do not establish physical VRAM/event/VMM residency, a driver cleanup contract,
or one-to-one attribution for every object from the full suite.

After those probes, the latest `gate-device` full rerun observed refcount zero
immediately after tests and before normal unload; the recorded check duration was
0.000175518 seconds. Test, rmmod and runner exits were zero, the owned module was
gone, and all existing socket/module/metadata preservation checks passed.
`cuda.shm.*` increased from **1,213 files / 99,368,960 bytes** to **1,309 files /
107,233,280 bytes**: +96 files / +7,864,320 bytes. This newer observation leaves
the original accumulation gate open despite all testcase assertions passing.

Latest evidence: `log/g3/completion/gate-device/{counts.json,device-session.json}`
and `log/g3/completion/assessment.json`; the latter records the open G3 verdict,
140-source reconstruction identity and 86 binary hashes. Earlier full-run evidence
remains in `full-device/` and `probe-counts.json`, and CPU results in
`cpu/counts.json`. The residue investigations are
`{mixed-event-residency,runtime-event-residency,vmm-residency,vmm-ipc-residency}/result.json`
under the same stage directory. The original no-unbounded-accumulation condition
still applies. `cuda.shm.*` objects remain outside application metadata cleanup;
safe deletion, PID/name reuse and bounded containment are unresolved. Private
namespace deployment is awaiting user selection and has not been validated. No
CUDA shared-memory object was deleted and no device reset was performed.
FD/metadata baselines alone cannot establish physical VMM/event/VRAM recovery or
payload copy absence. G3 remains open; G4/G5 and Orin validation remain separate
unfinished work. Finalize and Git mutation have not been run for this stage.

## Private namespace epoch containment (2026-10-31 KST, §35)

The user approved a fresh private IPC/mount namespace and `/dev/shm` for every
complete GPU peer-group epoch. G3 **PASS** applies to that operating contract on
the tested Thor host. The §34 OPEN verdict and its growing host-shm inventory
remain historical results for the earlier unisolated execution boundary.
The canonical core patch remains
`2e33677cd3f71ed718ab40ec491a32dabf987c79f0eb3426964c3777f892e715`.

| Check | Recorded result |
|---|---|
| Selected namespace follow-up | 4 targets, 68 collected = **68 PASS**, skips/failures/errors zero |
| Host recovery subset | **63 PASS**: 25 Group/CLI + 23 namespace worker + 15 isolated owner, included above |
| Actual GPU namespace subset | **5 PASS**, included above |
| Current CTest registration | 84 targets; the full current registry was not rerun in this follow-up |
| Earlier full core baseline | §34: 81 targets, 264 collected = **261 PASS + 3 multi-GPU SKIP**; not a new namespace-stage run |
| Earlier CPU baseline | 39 targets, 584 collected = **466 PASS + 118 device opt-in SKIP**; not rerun in this follow-up |
| G3 adoption | PASS for the approved private-epoch contract on the tested Thor host |

The default `recovery/run.py` CLI now owns a separate `IsolatedEpoch` for each
attempt. Its non-root owner launches a privileged bootstrap through sudo and
pins the announced worker with a pidfd, then validates its live PID/start-time handshake.
The worker unshares IPC/mount namespaces, makes mount propagation private and
mounts empty tmpfs at `/dev/shm`; the PID namespace is unchanged. Before starting
peers it restores the caller's UID/GID/groups, verifies zero capabilities, sets
`NoNewPrivs` and acts as a child subreaper. Worker `LD_PRELOAD` is rejected;
heaphook belongs only in each peer's command environment.

Every GPU holder and communicating Agnocast peer must be inside the epoch.
External peers, namespace/private-shm FD export and forking GPU users are outside
the contract. The worker runs one registered `Group`, waits for all direct peers,
then waits for adopted unlink daemons to exit naturally with status zero and for
`waitpid` to report `ECHILD`. A new attempt requires complete worker evidence and
the outer owner's independent actual-worker death confirmation. The sudo wrapper
exit alone cannot authorize restart. Ownership/registration failures, abnormal
daemon exit or incomplete teardown forbid retry; prior-stage adoption after
parent failure remains unsupported. The lower-level `Group` API does not itself
isolate its mount namespace.

The device cases exercised three standalone runtime IPC-event epochs, three
reader-SIGKILL restart cycles, and both `partial_unmap` and `event_destroy`
terminal-poison paths with a separate healthy pair continuing to checksum.
Poisoned peers exited with status 1; only after complete epoch teardown did a new
namespace accept a fresh checksum. A separate actual CLI case returned 1 from
its first runtime-event peer and 0 from its second; the CLI automatically spent
one restart and finished with exit zero. These are process/epoch recovery
observations, not repair of a live poisoned mapper.

All native peers in this stage used `AGNOCAST_BRIDGE_MODE=off` and
`AGNOCAST_NO_DISCOVERY_AGENT=1`. The §35 acceptance did not exercise bridge,
discovery-agent teardown, DDS participants, or general ROS launch processes.
The subsequent review found privilege-boundary and failed-shutdown gaps; §36
records the corrective revision rather than treating these historical PASS
counts as proof that the reviewed implementation was complete.

Across the verified run, **16 fresh namespaces** each began with an empty shm
inventory. **21 adopted unlink daemons** exited naturally with status zero.
Every worker reported `ECHILD`, and every outer owner recorded actual worker
death and verified teardown. Each final private inventory contained one
`cuda.shm.*` object of **81,920 bytes**; native epochs also retained the fixed
`agnocast_type_registry` directory. This inventory is observed before the final
worker exits. No namespace or private-shm FD escapes the worker tree, so its
last exit releases the private mount. Namespace IDs can be reused after teardown
and are compared for separation only while epochs are concurrently live.

The host inventory was unchanged at **1,309 files / 107,233,280 bytes** before and
after the verified suite. Module refcount was zero after tests and before normal
unload; the recorded refcount check took 0.000147898 seconds. Test, rmmod and
runner exits were zero, the owned module was unloaded, and all pre-existing
socket/module/metadata preservation checks passed.

Two unsuccessful preliminary trials are retained. The first used an incorrect
native binary path: one runtime-event case passed, three native cases failed
with three teardown errors, and no native GPU case ran. The next trial had one
PASS and three FAIL because its oracle rejected the fixed private
`agnocast_type_registry` directory despite complete worker/daemon teardown.
The corrected oracle accepts only that exact directory name with a positive
size bounded by 60 bytes; unexpected private names still fail. Both trials
preserved the host shm inventory and normally unloaded the module. They are not
added to the final PASS count or hidden as successful native runs.

Evidence is `log/g3/isolation/verified/{assessment.json,ctest-registry.json,
device-session.json,test_results/,peers/}`. The preliminary trials remain under
`log/g3/isolation/{device-first,device}/`. The earlier full core baseline is
`log/g3/completion/gate-device/`, and its CPU baseline is
`log/g3/completion/cpu/`. Selected target results, source/core identity and
the unrepeated baselines must not be combined into a claim that all current
targets ran in §35.

This acceptance contains the reproduced named-shm accumulation within the
approved epoch lifetime. It does not fix CUDA runtime cleanup or measure physical
VRAM/event/VMM residency, and unisolated execution remains outside the accepted
boundary. No driver name was manually deleted, no device reset was performed,
and no unlink daemon was killed. G4 Python GPU ownership, G5 performance/copy
profiling and Orin validation remain unfinished. Finalize and Git mutation were
not run in this stage.

## Review corrections and restricted revalidation (2026-10-31 KST, §36)

The review of the §34–35 diff found two privilege-boundary defects and incomplete
failure-path coverage. The earlier §35 completion statement was too broad. This
revision corrects the concrete defects; it does not mark every inherited LOW,
refactoring suggestion, or future G4 requirement as resolved. The operational
source of truth is [recovery/README.md](recovery/README.md).

The canonical patch is now
`e500ce18ffead8ad657739a3ec090ea2ca202aef540d4ec568acac52f88ac5f4`.
It changes two core files relative to §35: cleanup context activation logs a
numeric code without constructing an error string in a noexcept path, and the
`make_bound` header describes ownership after a failed wait correctly. The
patched inventory remains 23 files; zero-fuzz reconstruction matches all 140
source files. A new isolated-process C++ case injects allocation failure while
the cleanup worker requeues a NOT_READY event, then checks one failure/diagnostic,
process survival, poison and retained charged ownership. Both core and probe
were rebuilt for this revision.

| Review concern | Corrective change and evidence |
|---|---|
| Caller-writable Python executed as root | Standalone stdlib bootstrap installed at fixed root-owned path; `python -I -B`, root-owned non-writable ancestors, no caller imports/request parsing before permanent drop, source/installed byte identity checked |
| Requested supplementary groups and tmpfs size trusted | Bootstrap derives groups through NSS and capacity through host `statvfs`; those fields no longer occur in the privileged request interface |
| SIGKILL escalation reported as success | Only an actually sent SIGTERM is requested termination; host regression and actual two-peer TERM→KILL test require failure/CLI 1 |
| Worker death leaves direct peers alive | Parent-death SIGKILL plus post-prctl parent check; actual worker-only kill confirms both non-stdin-reading registered peers die; no restart |
| Unstated bridge/discovery settings | Effective peer environment must explicitly set bridge off/discovery disabled before stage creation; rejected settings have host coverage |
| Negative namespace behavior unmeasured | Actual CLI TERM, CLI KILL/EOF, wrapper KILL, worker KILL, KILL escalation and descendant timeout all run; synthetic peers use neither CUDA nor Agnocast |
| Repeated wait overwrites death evidence; wait timeout loses handles | Terminal error latch preserves the first evidence; timed waits retain ownership handles; EOF close errors still wait for worker death and prohibit retry |
| Latest Group did not exercise unpublished inventory | Native three-block publisher is explicitly SIGKILLed before publication; all six pinned resources are removed only after group death |
| Runner optimized mode removes assert gates | `-O`/`PYTHONOPTIMIZE` is rejected before stage/artifact/module side effects |
| G4 launch/shutdown/DDS assumptions omitted | §8 and PLAN now identify direct registered executable limits, spin-before-registration initialization, TERM/deadline/poison propagation, no registered-epoch reinitialization, and required DDS/RMW integration tests; these are not implemented G4 features |

The administrator-installed helper is
`/usr/local/libexec/rclpy-zc-gpu/namespace-bootstrap.py`; its bytes match the source
`recovery/bootstrap.py`. No sudoers policy was changed. The system interpreter,
standard libraries and account database are trusted. The bootstrap leaves stdin
untouched and execs the checkout worker only after dropping UID/GID/groups and
capabilities. The owner starts the privileged interpreter from `/`; the worker
restores the caller working directory only after validating its unprivileged
handoff. Worker namespaces and credentials were also checked independently through
`/proc`, not only from self-reported JSON.

| Execution | Result and scope |
|---|---|
| Core build / probe build | PASS / PASS |
| First full diagnostic selection | 85 CTest targets, **347 collected = 344 PASS + 3 multi-GPU SKIP**; all passed, including transport 37 and direct Group device 4 |
| Final recovery selection | 5 targets, **119 PASS** = Group/CLI 25 + owner 28 + worker 24 + bootstrap 30 + device 12 |
| Latest result per registered target | 86 targets, **360 collected = 357 PASS + 3 multi-GPU SKIP**, failures/errors zero; replace the five overlapping results with the final selection, do not add both runs |
| CPU `scripts/verify` | 12 packages, 39 targets, **584 collected = 466 PASS + 118 device opt-in SKIP**, failures/errors zero |
| Final isolated epochs | 23: 17 native/runtime-event epochs and 6 synthetic failure-control epochs |
| Final adopted native unlink daemons | 13, all exit 0; count may vary with daemon creation races and is not a fixed API invariant |
| Final synthetic timeout child | Natural exit 0 after test-owned release marker; forked child observed PDEATHSIG 0; never signalled |
| Both GPU sessions | Host CUDA shm **1,309 files / 107,233,280 bytes unchanged**, refcount 0, normal rmmod 0, existing module/sockets/metadata preserved |

This is a composed latest-target coverage record, not one uninterrupted run of all
86 targets after the final owner correction. The 85-target diagnostic suite ran
inside its own private IPC/mount namespace using the installed bootstrap and an
unprivileged diagnostic harness. It reported ECHILD and six naturally exited
adopted descendants; its 97 final private shm entries stayed inside that tmpfs.
This harness intentionally runs descendant test processes and is not the direct
registered-peer operational supervisor. The separate 12-case namespace target
exercises that operational supervisor. Test-driver source and complete commands
are preserved under `log/g3/review-fixes/`.

The six synthetic negative tests establish OS ownership/control behavior, not GPU
cleanup after worker SIGKILL. Native reader crash, terminal poison, runtime IPC
residue and unpublished-pool recovery remain separate real GPU cases. The
expected worker-kill case has no worker-result JSON; the owner independently
confirms death and rejects success. CLI SIGKILL has no owner/CLI result; its worker
handles EOF and drains. The descendant-timeout case deliberately records incomplete
teardown and denies restart before the synthetic child is released naturally.
Thus not all 23 epochs are successful teardowns, and missing failure evidence is
never counted as successful recovery.

Failed trials are preserved. `red-owner.xml` reproduces overwritten death evidence
and the absent timed-wait API; `red-eof-close.xml` reproduces the later EOF-close
exception. The first six OS cases stopped at an unsupported `/proc/.../children`
fixture query; the fixture now uses only explicitly known PID identities and the
worker's parent chain. In the first GPU session the new unpublished test expected
supervisor unlink after an ordinary stop, but peer cleanup had already removed
all six names. That was an oracle/scenario mismatch: the test now kills the owned
publisher explicitly, as required to exercise crash cleanup. The first namespace
run was 11 PASS/1 FAIL; the final selected run supersedes it. Both sessions preserved
external IPC and unloaded the module normally.

Evidence: `log/g3/review-fixes/{assessment.json,reconstruction.json,cpu-verify.log,
cpu/test_results/,verified/,final/,os-control/,os-control-fixed/}`. The assessment
pins HEAD, nested fork HEAD, source/core/binary hashes, installed helper bytes,
CUDA/driver/sudo versions and the exact source XML for every counted target.
The original external review is preserved unchanged; a narrow read-only check of
the corrected privilege/ownership paths found the additional EOF-close gap above,
which was fixed and regression-tested. No new broad four-reviewer report was run.

Restricted Thor G3 recovery is revalidated under the documented direct registered
peer, bridge-off/discovery-off, private-epoch contract. General ROS launch support,
G4 runtime shutdown and DDS/RMW integration, physical GPU residency/performance,
Orin/CUDA 12, long-running single-epoch event accumulation and aggregate tmpfs
quotas remain outside this validation. Older event-open VMM counter coverage,
numeric diagnostic assertions, packaging/license metadata and structural cleanup
proposals are not represented as closed. Finalize, commit, stage and push were
not executed for this revision.

## Shared checkpoint and device observations

Recorded transport checkpoints used 32 blocks, 64 warmup and 1,000 measured
deliveries, validating checksums and increasing per-block UIDs. Settled publisher
FD/metadata-map counts were 123/32 and subscriber counts 59/0 before and after.
These observations do not count physical VRAM, VMM mappings or CUDA events and
are not copy traces. Later transport suites also covered three controlled
crash/restart cycles while an unrelated pair continued to progress.

Device-session records identify verified ARM64 artifacts and temporary
`agnocast_rclpy` module loads. Peers exited and module refcount reached zero before
unload; the pre-existing `agnocast` module and 13 sockets were preserved. Cleanup
uses recorded test-owned names, not a namespace scan. Per-stage exceptions and
source/binary hashes remain in the associated raw evidence.
