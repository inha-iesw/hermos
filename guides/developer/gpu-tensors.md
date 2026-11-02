# PyTorch CUDA Tensor 사용하기

[프로젝트 README](../../README.md) · [환경 준비](setup.md)

GPU는 일반 ROS 메시지 안에 Tensor 데이터를 넣는 대신, GPU 버퍼에 직접 연산하고
`GpuTensor` descriptor를 전달합니다. descriptor에는 shape·dtype·공유 메모리와 동기화에
필요한 정보가 들어갑니다. 수신자는 이를 통해 같은 GPU 저장소에 접근합니다.

다음 코드를 import 가능한 `tensor_app.py`로 저장하는 형태로 애플리케이션을 구성합니다.
launcher가 `rclpy`와 `GpuRuntime`을 초기화하고, `factory()`가 반환한 Node를 등록 후 실행합니다.
`factory()` 안에서 직접 `spin()`, `publish()`, `runtime.register()`를 호출하지 않습니다.

```python
import torch
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy_hermos import Node, SubscriptionLimits
from rclpy_hermos_gpu_msgs.msg import GpuTensor


def factory(runtime, arguments):
    role = arguments[0]  # "pub" 또는 "sub"
    node = Node(f"tensor_{role}", context=runtime.ros_context)
    qos = QoSProfile(depth=4, reliability=ReliabilityPolicy.BEST_EFFORT)
    # runtime이 pending GPU 작업을 정리할 때까지 stream을 유지합니다.
    node.gpu_stream = torch.cuda.Stream(device=0)

    if role == "pub":
        publisher = node.create_publisher(
            GpuTensor, "/tensor", qos,
            transport="hermos",
            max_message_bytes=4096,  # CPU descriptor 한도
        )

        def publish():
            with publisher.borrow_tensor(
                (32, 32), torch.float32, "cuda:0", node.gpu_stream
            ) as loan:
                tensor = loan.as_tensor()
                with torch.cuda.stream(node.gpu_stream):
                    tensor.fill_(7.0)  # 공유 GPU 버퍼에 예제 데이터를 생성
                del tensor
                publisher.publish(loan)

        node.create_timer(1.0, publish)

    elif role == "sub":
        def consume(lease):
            tensor = lease.as_tensor(stream=node.gpu_stream)
            with torch.cuda.stream(node.gpu_stream):
                total = tensor.sum().item()
            node.get_logger().info(f"sum={total}")

        node.create_subscription(
            GpuTensor, "/tensor", consume, qos,
            transport="hermos",
            limits=SubscriptionLimits(
                max_messages=4, max_retained_bytes=16384, max_message_bytes=4096,
            ),
        )
    else:
        raise ValueError("role must be pub or sub")

    return node
```

수신 로그의 예상 합계는 `7168.0`입니다. `.item()`은 이 합계 한 개를 CPU로 가져오는
동기화·복사입니다. 전송 중 Tensor payload 전체를 CPU에 옮기는 동작과는 구분하세요.

## GPU peer 실행 설정

Publisher와 subscriber를 **같은 supervisor 그룹의 서로 다른 프로세스**로 실행합니다.
다음은 supervisor의 `commands[].argv`에 넣을 peer 명령입니다.
등록 환경을 생략한 독립 실행이나 일반 `ros2 run/launch`의 대체 명령으로 사용하지 않습니다.

Publisher (`pub`):

```text
/usr/bin/python3 -u -m rclpy_hermos_gpu.launch --blocks 4 --block-bytes 4096 --max-reads 8 --max-message-bytes 4096 tensor_app:factory pub
```

Subscriber (`sub`):

```text
/usr/bin/python3 -u -m rclpy_hermos_gpu.launch --blocks 0 --block-bytes 4096 --max-reads 8 --max-message-bytes 4096 tensor_app:factory sub
```

`--blocks 0`은 수신 전용 프로세스를 뜻합니다. `tensor_app.py` 디렉터리를 각 peer의
`PYTHONPATH`에 포함하고, [환경 준비](setup.md)에서 설명한 장치·heap hook 환경도 각 peer에 설정합니다.
`LD_PRELOAD`는 supervisor 전체에 적용하지 않습니다. supervisor JSON은 shell이 아니므로
환경·실행 파일 경로는 실제 값으로 작성해야 합니다.

supervisor 설정·시작·종료 방법은 [운영 가이드](../../phase4_gpu_core_probe/repair/recovery/README.md),
API와 stream 계약은 [GPU 가이드](../../rclpy_hermos_gpu/README.md)를 따릅니다.

## GPU 버퍼와 수명 규칙

- `(32, 32)` float32의 payload는 4096바이트입니다. shape는 바꿀 수 있지만 현재는
  runtime 생성 시 정한 block 크기 안에 들어가야 합니다. 무제한 동적 GPU 할당은 아닙니다.
- Node의 `max_message_bytes`와 `SubscriptionLimits`는 **CPU descriptor**에 적용됩니다.
  launcher의 `--blocks`, `--block-bytes`, `--max-reads`, `--max-message-bytes`는
  별도의 GPU pool·읽기 자원 설정입니다. `--max-message-bytes`는 mapper가 수용할
  GPU allocation의 최대 바이트 수이며, CPU descriptor 크기와 다릅니다.
- 발행 전 writable Tensor뿐 아니라 slice·detach 등 저장 공간을 공유하는 모든 alias를
  해제해야 합니다. 발행 후 loan이나 CPU 메시지 proxy에 다시 접근하지 않습니다.
- 수신 Tensor는 읽기 전용으로 사용합니다. CUDA mapping은 쓰기를 하드웨어로 차단하지 않습니다.
- 마지막 alias와 지정 stream의 작업이 끝나야 저장소를 재사용할 수 있습니다.
  다른 stream에서 사용했다면 그 완료를 지정 stream에 연결해야 하며,
  `Tensor.record_stream()`만으로 외부 공유 버퍼 수명이 관리되지는 않습니다.
- 현재 GPU facade는 dense/offset-zero layout을 사용합니다. bool, bfloat16, complex dtype이나
  임의 stride layout을 자동 변환해 주지 않습니다.

오류 해결은 [문제 해결 가이드](troubleshooting.md)를 참고하세요.
