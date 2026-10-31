# Owned GPU peer recovery

This README is the authoritative operational contract for the recovery CLI and
API. The parent README and plans summarize it; [EVIDENCE.md](../EVIDENCE.md)
records the exact source revisions, executed scenarios and their limits.
The review corrections passed the §36 host and device/OS verification on Thor.
That evidence covers the restricted registered-peer contract below, not general
ROS launch, the future Python GPU runtime, or every deferred review proposal.

`run.py` supervises a complete GPU peer group in a **fresh IPC/mount namespace and
private `/dev/shm` for every epoch**. A fixed, administrator-installed bootstrap
makes mount propagation private, mounts tmpfs and enables child subreaping.
It permanently drops to the sudo caller's UID/GID, obtains supplementary groups
from the system account database, verifies zero capabilities and sets
`NoNewPrivs` before executing the unprivileged worker. Caller JSON cannot choose
groups or the tmpfs size. The PID namespace is unchanged.

## Administrator installation

The privileged entry point is exactly
`/usr/local/libexec/rclpy-zc-gpu/namespace-bootstrap.py`. It and every ancestor
directory must be root-owned, not group/other-writable, and not symlinks.
The supervisor also requires its bytes to match the reviewed `bootstrap.py`
source. An administrator must review and install an updated helper when that
source changes; a caller cannot substitute a checkout path as the privileged
entry point. From this workspace, the administrator installation commands are:

```bash
sudo install -d -o root -g root -m 0755 /usr/local/libexec/rclpy-zc-gpu
sudo install -o root -g root -m 0644 \
  phase4_gpu_core_probe/repair/recovery/bootstrap.py \
  /usr/local/libexec/rclpy-zc-gpu/namespace-bootstrap.py
```

Run the CLI as a non-root user. If granting a dedicated sudo rule, the
administrator must restrict it to the fixed system interpreter, options and
installed helper; for example, replace `ros` with the approved account:

```sudoers
ros ALL=(root) NOPASSWD: /usr/bin/python3 -I -B /usr/local/libexec/rclpy-zc-gpu/namespace-bootstrap.py *
```

Do not grant a generic Python command or a caller-writable checkout script.
The privileged process starts from `/`, uses Python isolated mode `-I`, imports
only the system standard library, and does not read caller request JSON or
write caller paths. It accepts absolute worker/stage paths but reaches the
checkout worker only through `exec` after the permanent privilege drop.
The system interpreter, its libraries and account database are administrator
trust boundaries. This installation step does not install a general ROS launcher
or turn the probe package into a production GPU runtime.

## Supported peer group

The group must contain every holder of its GPU publications/imports and every
Agnocast peer that communicates with it. Outside peers must not share its GPU
publications or Agnocast IPC. The FD sockets under `/tmp` remain in a shared
filesystem and have no peer-credential admission policy: this is a contract among
trusted peers, not a security boundary against another process with the same UID.
Attaching to external processes, forking GPU users, entering another namespace,
passing namespace/private-shm FDs outside, and pool reinitialisation after
registration are outside this contract. A process that does not register cannot
participate. A replacement supervisor must not adopt an old stage or infer
ownership from global scans. Parent loss requests orderly shutdown through pipe
EOF; abrupt loss is not a persistent recovery/adoption service. Each direct peer
has a parent-death SIGKILL and rechecks its parent immediately after installing
it. Worker death therefore terminates those direct peers, but may prevent name
cleanup and final evidence. It forbids restart and adoption. The parent-death
signal is not inherited by forked unlink daemons; they are never killed by this
policy and must exit naturally.

The supported commands are direct executables implementing the registration
protocol, currently the probe peers. `ros2 run`, `ros2 launch`, stock nodes and
G1/G2 peers without that protocol are not supported launch forms. Every peer's
effective environment must contain `AGNOCAST_BRIDGE_MODE=off` and
`AGNOCAST_NO_DISCOVERY_AGENT=1`; `IsolatedEpoch` rejects missing or different
values before creating a stage. The checked environment includes inherited
variables plus each command's overrides. Bridge/discovery child lifetimes have
not been adopted under this contract. DDS connectivity within or across epochs
is also unvalidated; the Agnocast/GPU restriction is not a DDS transport policy.

`LD_PRELOAD` belongs in each command's `env`, never in the supervisor environment.
The worker itself must not initialise Agnocast: its registration would keep the
unlink daemon alive. Peer environments inherit the caller environment over a pipe
after privilege drop; the inherited environment is not written to evidence files.
The bootstrap measures the existing `/dev/shm` capacity itself before isolating
mounts and uses that value as the per-epoch tmpfs limit. It does not preallocate
those bytes or impose a combined limit on concurrent epochs. Account group
membership is derived through `pwd.getpwuid`/`os.getgrouplist`, not copied from
the caller's pipe. Stages and the working directory must be outside
`/dev/shm`, and each stage must be a new absolute path.

The lower-level API is single-owner and single-threaded. `Group(commands, absolute_stage,
startup_timeout=10, stop_timeout=5).start()` waits for all registrations and then
acknowledges them. `processes` exposes the current `Popen` objects for stdin
interaction and exit observation; callers must not reap, wait or signal them
independently. `epoch_dir` contains each peer's stdout/stderr. `stop()` terminates
the entire group, cleans registered names after confirmed death, and returns its
epoch evidence. A pipe/log/FD close error is recorded after attempting all cleanup
and prevents restart even if IPC cleanup succeeds. `restart()`
performs that stop before launching a fresh epoch in the same namespace. `Group`
is the worker/test primitive and provides no mount isolation itself. The G3
operational entry point is the isolated CLI or `IsolatedEpoch` below.

```json
{"commands": [
  {"argv": ["/absolute/publisher", "arg"], "env": {
    "AGNOCAST_BRIDGE_MODE": "off", "AGNOCAST_NO_DISCOVERY_AGENT": "1"}},
  {"argv": ["/absolute/subscriber", "arg"], "env": {
    "AGNOCAST_BRIDGE_MODE": "off", "AGNOCAST_NO_DISCOVERY_AGENT": "1"}}
]}
```

```bash
python3 phase4_gpu_core_probe/repair/recovery/run.py peers.json \
  --stage /absolute/new-recovery-stage --startup-timeout 10 \
  --stop-timeout 5 --max-restarts 1
```

## Registration, termination and evidence

Each native peer atomically writes its `G3_RECOVERY_MANIFEST` after initialisation:
`{"version":1,"pid":123,"resources":["/dev/shm/rclpy_zc_gpu_v1_123_0"]}`.
It must wait for `G3_RECOVERY_ACK` before publishing/importing. The supervisor
checks the child PID and matching PID/IPC/mount namespaces, and records inode/device,
type and owner. Each resource is pinned with `O_PATH|O_NOFOLLOW` until confirmed
group death and the cleanup attempt complete; `fstat` and pathname `lstat` must
agree at registration. Keeping the inode alive also prevents inode-number reuse
after a peer normally unlinks its name. Only regular metadata files for that child's PID and UNIX socket
names under `/tmp/cuda_memory_pool_<uuid>.sock` are accepted. Symlinks, foreign
owners, duplicate names and arbitrary paths are rejected. Subscribers may
register an empty inventory. The parent is the sole owner of the stage, and no
external actor may replace the registered IPC names during the epoch or cleanup.

Any failed peer triggers whole-group TERM, bounded waiting, then pidfd KILL if
needed. Every child must be waited before names are removed. Changed inodes
prevent cleanup and restart. An absent name is allowed after normal peer cleanup.
Missing/invalid startup registration terminates all children but preserves even
partially registered names and forbids restart. If opening a child's pidfd fails,
the sole owner kills and waits that still-unreaped direct child before recording
the startup failure; this fallback never enters a runnable epoch.

The CLI observes process exits, not internal CUDA poison state. An application
must stop GPU work and exit nonzero after terminal poison, or its API owner must
stop the epoch and create a fresh one. Keeping a poisoned peer alive without reporting its
state does not trigger the CLI. Device regressions exercise both synchronous
import rollback poison and asynchronous event cleanup poison, followed by peer
error exit and complete group restart; the next epoch disables only the test's
fault-injection setting.

Each `epoch-N/group/recovery.json` records PID, registration inventory, signals,
exit codes and application cleanup. `worker.json` records namespace and privilege
setup; `worker-result.json` records peer failure, every adopted descendant's
return code, `ECHILD`, and private shm inventory. `owner.json` records the outer
owner's independent worker-death confirmation and observed registered-peer
identity/liveness states after a failed teardown. `cli.json` records restart count
and CLI exit status. Bootstrap failure before verified privilege drop is written
only to inherited stderr, never to caller paths with root privilege.
The restart budget defaults to one and is not applied to registration/ownership
failures. The first normal peer exit also stops the remaining group and completes
normally unless another peer reports an independent error. A negative exit code
is treated as requested termination only for SIGTERM successfully sent to that
specific peer. SIGKILL escalation is a peer failure, not successful shutdown;
the per-peer evidence records each sent signal.
SIGINT/SIGTERM requests orderly group shutdown; an ongoing startup may take up to
its configured deadline before that request is processed. Control and peer-stdin
writes are nonblocking with deadlines, so a peer that stops reading cannot freeze
the cleanup monitor. A control failure still attempts EOF and complete teardown,
records the error, and forbids retry.

After Group has waited every direct peer, the worker waits for adopted Agnocast
unlink daemons to exit naturally with status zero and for `waitpid` to report
`ECHILD`. It never kills a daemon. A timeout, abnormal daemon exit, incomplete
registration or cleanup failure forbids a new epoch. The outer owner pins the
actual worker's PID with a pidfd after an identity/start-time handshake; the sudo
wrapper's PID or exit status alone is not evidence of worker death. The owner
opens the worker pidfd before validating its start time and dropped UID.
Only complete worker evidence plus actual worker death permits a new namespace after peer
failure. Namespace IDs may be reused once the previous namespace is destroyed.

## API and remaining integration work

`IsolatedEpoch(commands, absolute_stage, startup_timeout=10, stop_timeout=5)`
exposes `start()`, `send(peer_index, line)`, `poll()`, `wait(timeout=None)` and `stop()`.
`peers` lists PIDs for observation, and `epoch_dir` contains stdout/stderr. Do not
reap or wait these peers; the worker is their sole owner. `poll()` observes the
sudo wrapper only. `wait()` without a timeout permits a long-running epoch;
an explicit timeout raises without closing stdin or the worker pidfd, leaving
`stop()` available. Once the wrapper exits, verification still requires worker
termination and complete evidence. Terminal verification failures are retained:
another `wait()`/`stop()` does not overwrite the first failure evidence. Each instance
starts once; create a new instance/stage only after verified teardown. Automatic
failure retry additionally requires `retry_allowed`. `stop()` requests whole-group shutdown and waits for the same
boundary. Setup/ownership errors cannot be converted into a successful epoch by
calling `wait()` again.

`stop()` budgets for startup, a pending peer-input write, TERM waiting, KILL
waiting and natural descendant drain. The configured `stop_timeout` also bounds
that last drain. A value below the unlink daemon's roughly one-second polling
period can reject otherwise normal cleanup; the tested default is five seconds.
A teardown timeout means incomplete cleanup and never permission to restart;
an observation-only `wait(timeout=...)` leaves the epoch running and owned.

G4 must connect TERM to runtime shutdown, choose a peer shutdown deadline shorter
than the supervisor's TERM wait with scheduling margin, and own the ordering
dispatch stop → mapper seal/drain → producer close → CUDA teardown. It must
propagate asynchronous terminal poison to a nonzero process exit, reject pool
reinitialisation after registration, and finish explicit GPU runtime setup and
registration before spin. The probe's default TERM exit does not validate that
future Python/runtime sequence. Resource-free launchers, general ROS launch and
inside/outside-epoch DDS tests for both RMWs remain G4 integration work.

Runtime IPC events leave driver-owned objects on the tested CUDA combination;
see [the standalone diagnostics](../residency/README.md). They and the fixed
Agnocast type registry remain inside the epoch's tmpfs until its final holder
exits. No driver name is manually unlinked, no CUDA reset is used, and no mount or
namespace FD is exported to the owner. The kernel releases the private mount
after the worker and descendants exit. Evidence inventories private residue before
that final exit; it does not claim that CUDA's internal cleanup has been repaired
or measure physical VRAM. Existing host objects are preserved. The original
no-unbounded-accumulation criterion applies to this bounded execution contract;
unisolated diagnostic runs do not satisfy it on the tested driver.
The unpublished three-block cleanup test covers registered inventory removal
after publisher SIGKILL. Short epoch tests do not establish an
event-count-independent bound during a long-running epoch, general block-count
scaling, aggregate tmpfs limits across many groups, or physical GPU-memory
recovery. The OS failure controls use synthetic non-GPU peers to test restart
refusal without introducing orphan CUDA resources; they do not establish native
CUDA daemon behavior after worker death. Those limits remain explicit until the
corresponding scenario or metric is measured.
