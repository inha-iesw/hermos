# CPU 메시지 사용하기

[프로젝트 README](../../README.md) · [환경 준비](setup.md)

## 일반 rclpy 코드에서 달라지는 점

| 항목 | 일반 rclpy | HERMOS CPU 메시지 |
|---|---|---|
| Node import | `from rclpy.node import Node` | `from rclpy_hermos import Node` |
| 통신 선택 | 기본 DDS | endpoint에 `transport="hermos"` 명시 |
| QoS | 정수 depth 또는 QoSProfile | 명시적 `KEEP_LAST / BEST_EFFORT / VOLATILE` QoSProfile |
| 발행 메시지 | `Image()` 등 Python 메시지 생성 | `publisher.borrow_message()`로 공유 메시지를 빌림 |
| 데이터 작성 | Python 객체에 대입 | 빌린 메시지의 필드·버퍼에 직접 작성 |
| 수신 데이터 | Python 메시지 객체 | 읽기 전용 native 메시지 proxy |
| 자원 한도 | HERMOS 설정 없음 | publisher의 `max_message_bytes`, subscriber의 `SubscriptionLimits` |

`transport`를 생략하면 DDS를 사용합니다. 따라서 기존 DDS endpoint와 HERMOS endpoint를
같은 Node에서 선택해 사용할 수 있습니다. 이 선택이 DDS↔HERMOS 자동 bridge를 만드는 것은 아닙니다.
HERMOS publisher에 일반 `Image()` 객체를 전달하거나, 받은 메시지를 그대로 다시 발행하는 방식은
지원하지 않습니다.

기존 코드를 이전할 때는 import뿐 아니라 ROS 패키지 의존성, generated adapter,
peer 실행 파일도 함께 맞추세요. 설치 wrapper가 peer 환경을 준비합니다.
이전 이름에 대한 호환 alias는 제공하지 않습니다.

## 앱을 ros2 run/launch에 등록하기

관리자가 [CPU 모듈·장치 권한](setup.md#설치된-cpu-앱-실행)을 준비한 환경에서 사용합니다.
앱의 `main()`은 ROS 인자를 포함한 `sys.argv`를 받아 기존처럼 `rclpy.init()`을 호출하고,
종료 시 자신의 endpoint·alias·Node를 정리합니다. 앱 entry module을 bootstrap 전에 import하지 않습니다.

`ament_cmake` 앱은 `package.xml`에 `hermos_runtime`과 필요한 adapter 의존성을 선언한 뒤
다음 helper로 실행 파일을 설치합니다. 앱 Python package 자체의 설치는 기존 빌드 규칙을 따릅니다.

```cmake
find_package(hermos_runtime REQUIRED)
hermos_install_cpu_executable(camera_pub
  PYTHON_ENTRY my_pkg.camera:main
  PROVIDERS rclpy_hermos_adapters)
# C++ 앱은 PYTHON_ENTRY 대신 이미 정의한 executable TARGET을 지정합니다.
```

`ament_python` 앱은 console entry point를 앱을 import하지 않는 bootstrap 함수에 연결합니다.

```python
def camera_pub():
    from hermos_runtime.cpu import run
    return run('my_pkg.camera:main', providers=['rclpy_hermos_adapters'])
```

이 wrapper를 `lib/<app_package>/<executable>`에 설치하면 `ros2 run my_pkg camera_pub`로
실행할 수 있습니다. launch에서는 `hermos_launch` 의존성을 추가하고 다음처럼 사용합니다.

```python
from hermos_launch.actions import HermosNode

camera = HermosNode(
    package='my_pkg', executable='camera_pub',
    namespace='camera', remappings=[('image', 'frames')],
    parameters=[{'label': 'camera with spaces'}],
    providers=['rclpy_hermos_adapters'],
)
```

표준 Node의 이름·namespace·remap·parameter·arguments 처리를 사용하며 환경은 해당 peer에만
적용합니다. 일반 DDS Node나 `ros2` CLI 전체에 heap hook을 적용하지 않습니다.
`ROS_DOMAIN_ID`는 같은 프로세스의 Python/native runtime이 함께 사용하므로
`rclpy.Context.init(domain_id=...)`로 환경과 다른 domain을 별도로 지정하지 않습니다.

## Image 발행·구독

각 예제는 **별도 프로세스**에서 사용합니다. `rclpy.init()`과 [CPU 실행 환경](setup.md)이 준비된
상태에서 Node를 생성하고, endpoint 설정 후 executor를 실행하는 구조입니다.
일반 `rclpy`의 timer·callback 작성 방식은 유지합니다.

### Publisher

8×8 흑백 이미지를 공유 버퍼에 직접 작성합니다. 이 루프는 API 설명용입니다.
실제 영상 처리에서는 데이터를 생성하는 연산이 공유 버퍼를 출력 대상으로 사용하도록 연결하세요.

```python
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy_hermos import Node
from sensor_msgs.msg import Image

node = Node("image_writer")
qos = QoSProfile(depth=4, reliability=ReliabilityPolicy.BEST_EFFORT)
publisher = node.create_publisher(
    Image, "/image", qos,
    transport="hermos",
    max_message_bytes=65536,
)


def publish_image():
    width, height = 8, 8
    with publisher.borrow_message() as message:
        # 크기 변경을 모두 끝낸 다음 값을 씁니다.
        message.encoding.resize(5)
        message.data.resize(width * height)

        message.width = width
        message.height = height
        message.step = width
        message.is_bigendian = 0
        with message.encoding.buffer() as encoding:
            encoding[:] = b"mono8"
        with message.data.buffer() as pixels:
            for i in range(len(pixels)):
                pixels[i] = i % 256

        # 두 writable view의 with 블록을 벗어난 뒤 발행합니다.
        publisher.publish(message)


timer = node.create_timer(0.1, publish_image)
```

`resize()`는 가변 길이 필드에 **이번 메시지에서 필요한 길이**를 지정하는 작업입니다.
메시지마다 길이가 달라도 되며 고정 크기 메시지로 바꾸는 연산이 아닙니다.
문자열과 배열을 포함한 모든 `resize()`는 첫 값 쓰기나 `buffer()` 호출 전에 끝내야 합니다.
크기를 바꾸면 이전에 얻어 둔 하위 필드 proxy는 무효가 되므로 root에서 다시 접근하세요.

### Subscriber

```python
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy_hermos import Node, SubscriptionLimits
from sensor_msgs.msg import Image

node = Node("image_reader")
qos = QoSProfile(depth=4, reliability=ReliabilityPolicy.BEST_EFFORT)


def receive(message):
    with message.data.buffer() as pixels:
        node.get_logger().info(
            f"{message.width}x{message.height}, "
            f"encoding={message.encoding.to_text()}, bytes={len(pixels)}"
        )


subscription = node.create_subscription(
    Image, "/image", receive, qos,
    transport="hermos",
    limits=SubscriptionLimits(
        max_messages=4,
        max_retained_bytes=262144,
        max_message_bytes=65536,
    ),
)
```

수신 `buffer()`는 읽기 전용입니다. `to_text()`는 문자열을 Python 문자열로 복사·변환하는
명시적인 편의 API이며, 이미지 payload를 복사하지는 않습니다.

종료할 때는 callback과 보유 중인 메시지·view·Tensor를 정리한 뒤 `node.destroy_node()`와
ROS context 종료를 수행하세요. 아직 보유한 객체가 있으면 Node 종료가 busy 오류로 실패할 수
있으며, 해당 객체를 해제하고 이미 시작된 callback을 마친 뒤 재시도해야 합니다.

### 한도와 수명

| 설정 | 의미 |
|---|---|
| publisher `max_message_bytes` | 발행 메시지 하나의 native root와 동적 container capacity를 합산한 저장 공간 한도 |
| `SubscriptionLimits.max_messages` | 대기 중이거나 수신자가 보유 중인 메시지 수 한도 |
| `max_retained_bytes` | 수신 측이 보유 중인 메시지들의 합산 저장 공간 한도 |
| subscriber `max_message_bytes` | 메시지 한 건을 수신할 때 허용하는 저장 공간 한도 |

이 값은 순수 payload 길이, 전체 RSS 또는 GPU 메모리 사용량과 같지 않습니다.
현재는 양의 정수 한도를 명시해야 하므로 **설정한 한도 안에서 가변 길이**를 지원합니다.
한도는 수신 허용량을 정하는 정책이며 메시지를 강제로 회수하는 장치가 아닙니다.
callback이 끝나도 메시지나 파생 view를 보유하면 저장소와 한도 사용량이 유지됩니다.

### NumPy와 CPU Torch Tensor

위 버퍼 예제는 NumPy를 직접 import하지 않습니다. 다만 현재 `rclpy_hermos` 패키지에는
`python3-numpy` 런타임 의존성이 선언되어 있습니다. NumPy의 `fill()`이나 Torch의
`fill_()`는 데이터 생성 도구이며 HERMOS 통신에 필수인 메서드는 아닙니다.

숫자 배열 필드를 CPU Tensor로 보고 싶다면 다음처럼 사용할 수 있습니다.
아래 `message`는 위 예제의 `mono8`, `step=width` Image 메시지입니다. RGB나 행 padding이
있는 이미지는 실제 데이터 배치에 맞는 shape를 사용해야 합니다.

```python
# Torch가 설치되어 있어야 합니다. 데이터 복사 없이 공유 CPU 배열을 봅니다.
tensor = message.data.as_tensor(shape=(message.height, message.width))
mean = tensor.float().mean().item()  # float() 변환과 연산 결과는 별도 저장 공간을 사용합니다.
del tensor
```

CPU 수신 Tensor에 in-place 쓰기를 하지 마세요. 실제 공유 메모리의 읽기 전용 보호에
걸릴 수 있습니다. 수정할 데이터가 필요하면 명시적으로 `clone()`하여 사용합니다.
일반 메시지·버퍼 API만 사용할 때는 Torch가 필요하지 않습니다.

다른 메시지 타입은 [사용자 정의 메시지 연결](custom-messages.md), 오류 해결은 [문제 해결 가이드](troubleshooting.md)를 참고하세요.
