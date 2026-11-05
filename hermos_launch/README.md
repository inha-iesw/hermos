# Registered groups in ROS launch

`hermos_launch.actions.HermosPeerGroup` starts one installed supervisor. Describe
its direct peers with `hermos_launch.description.HermosPeer`; do not nest Node
actions or per-peer respawn. For an application that supplies registered factories:

```python
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from hermos_launch.actions import HermosPeerGroup
from hermos_launch.description import HermosPeer

def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('camera_parameters', description='Existing ROS parameter YAML'),
        HermosPeerGroup(peers=[
            HermosPeer(peer_id='writer', role='gpu', factory='my_pkg.camera:factory',
                arguments=['camera-0'], name='camera', namespace='robot',
                remappings=[('image', 'frames')],
                parameters=[LaunchConfiguration('camera_parameters')],
                pool=dict(blocks=4, block_bytes=4096, max_reads=8, max_message_bytes=4096)),
            HermosPeer(peer_id='control', role='cpu', factory='my_pkg.control:factory',
                providers=['rclpy_hermos_adapters']),
        ], startup_timeout=30, stop_timeout=5, max_restarts=1),
    ])
```

Pass `camera_parameters:=/path/to/camera.yaml` when invoking this launch file.
`parameters` accepts existing file paths/substitutions; inline dictionaries
are not supported. Scoped namespace, remaps and parameters are also passed through.
Pool values accept launch substitutions and are fixed at runtime initialization.
Choose capacity for peak outstanding loans, retained aliases and stream completion
time; the example sizes above are not application sizing defaults. Exhausted pools
retain their existing error/backpressure behavior; launch does not resize them.

For a native registered peer use `package='my_pkg', executable='gpu_peer',
registered=True, role='gpu'`. The executable must implement the common registration,
ACK and teardown contract itself. The namespace worker resolves it directly under
`lib/<package>` using that peer's selected overlay. This is not a CPU bootstrap wrapper
or an arbitrary stock ROS executable. Metadata peers use `role='metadata'`, register
an empty inventory and do not preload the heap hook. This option serves external
applications implementing that protocol; the backend discovery agent's E3 integration
is still pending and must avoid a backend-to-runtime dependency cycle. Direct peers
handle INT/TERM with graceful drain and exit zero on success.

The action validates installed artifacts/device/providers, configures only payload
peer environments and passes application and ROS arguments separately. The supervisor
and worker load no native/GPU runtime. CPU/GPU factories share factory parsing,
ROS argument/context initialization and Node/context validation. GPU factories run
through the installed `rclpy_hermos_gpu/hermos_gpu_peer` executable, so the launch
working directory cannot shadow the launcher via `python -m`.

`additional_env` is scoped to this group. Its selected runtime prefix must agree
with the imported runtime and supervisor entry; mixing runtime revisions is rejected.
Payload/app package overlays may still select their direct executables. All package
executables are checked before creating a supervisor or starting any peer.
For CPU/GPU factories, a preload-free system-Python inspector also checks the actual
child environment's native-module search result against the verified installation,
without importing the payload package or native extension. Explicit `PYTHONPATH`
skew is rejected; raw registered native executables keep their direct contract.

Ordinary DDS Nodes can appear beside the action;
Fast DDS across private shm mounts requires the explicit UDP profile in the
[GPU transport guide](../rclpy_hermos_gpu/README.md) on both sides.

Each invocation creates a private fresh directory beneath the launch log directory
(or `log_directory`). It retains `commands.json`, supervisor `cli.json`, per-epoch
owner/worker evidence and `peer-N.stdout`/`peer-N.stderr`. Lifecycle output maps peer
IDs to these logs and distinguishes supervisor starting, registration-ready and
stopped/incomplete. Registration-ready does not announce application consumer readiness.
Incomplete teardown or unexpected supervisor exit fails the launch. Only the supervisor
may restart, after confirmed teardown, within `max_restarts`.
Confirmed process/resource cleanup is separate from successful peer drain: a peer's
nonzero drain exit or forced KILL still fails launch during Ctrl-C shutdown. The final
`peer_failed` flag describes the latest epoch, including after a successful restart.

Ctrl-C requests normal drain. For interactive launch, the action explicitly forwards
INT to its direct supervisor even when only the launch PID received the signal.
Repeated INT is idempotent; evidence-free early interruption still fails conservatively.
Launch-parent death also requests supervisor drain;
supervisor/worker loss retains the existing EOF/pidfd/parent-death containment. With
startup `A`, stop `S`, and worker wait `B=A+4S+2`, launch allows `A+4S+4B+5` seconds
before TERM and another five before KILL. This covers startup, both cleanup attempts
and ordinary evidence processing, not unbounded filesystem stalls. At defaults this
is 163 seconds plus five. GPU peers retain their separate three-second drain deadline.

The installed finite [GPU pipeline example](test/scenarios/gpu/) combines
two GPU factories, one registered CPU factory and a normal DDS Node. Build it after the
GPU overlay described in the [GPU build guide](../rclpy_hermos_gpu/README.md#build-and-verification), then run it with a fresh output directory and administrator setup:

```bash
colcon build --base-paths hermos_launch/test/scenarios/gpu \
  --build-base build/hermos/gpu-examples --install-base install/hermos/gpu-examples
source install/hermos/gpu-examples/local_setup.bash
ros2 launch hermos_gpu_examples gpu_pipeline.launch.py output_directory:=/tmp/my-new-gpu-run
```

The complete request, registration and lifecycle JSON contracts are in
[hermos_runtime supervision](../hermos_runtime/README.md). This action
uses version 1 configuration and lifecycle records; it does not wrap stock nodes
into registered peers or change public payload lifetime rules.
