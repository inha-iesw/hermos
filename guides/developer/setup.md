# 환경 준비와 빌드

[프로젝트 README](../../README.md) · [CPU 메시지](cpu-messages.md) · [CUDA Tensor](gpu-tensors.md)

## 사용할 수 있는 범위

| 목적 | 사용할 API / 패키지 |
|---|---|
| 기존 ROS 2 DDS 통신 | `rclpy_hermos.Node`의 기본 `transport="dds"` |
| CPU 메시지 zero-copy | `transport="hermos"`, `borrow_message()`, 필드의 `buffer()` |
| CPU 메시지의 숫자 배열을 Torch로 사용 | 필드의 `as_tensor()` |
| PyTorch CUDA Tensor zero-copy | `rclpy_hermos_gpu`, `GpuTensor`, `borrow_tensor()` |
| 기본 메시지 adapter | `sensor_msgs/msg/Image`, `sensor_msgs/msg/PointCloud2` |
| 사용자 정의 메시지 adapter 생성 | `rclpy_hermos_codegen` |
| C++ 연동 | CPU backend의 `hermoslib`와 GPU native core; Python과 같은 API는 아님 |

현재는 **소스 빌드와 실행 환경 준비가 필요한 개발 단계**입니다. 실장치 근거는 Thor의
Ubuntu 22.04 / ROS 2 Humble 컨테이너 환경을 중심으로 축적되어 있습니다. 임의의 ROS 2
배포판·GPU 조합이나 일반 `ros2 launch` 배포가 검증된 상태는 아닙니다.
GPU 전송은 같은 supervisor에 등록된 별도 프로세스끼리 사용하는 구성을 기준으로 합니다.

## 필요한 구성 요소

다음 구성 요소가 필요합니다.

| 구성 요소 | 용도 |
|---|---|
| ROS 2 Humble, Python 3.10, colcon, CMake 3.20 이상, C++ 빌드 도구 | 메시지 타입, Python API, native extension·CPU wrapper 빌드 |
| `hermos/` backend 소스, Rust/Cargo | 공유 힙 runtime과 heap hook 빌드 |
| 호스트 커널에 맞는 모듈과 장치 접근 권한 | CPU 프로세스 간 공유 메모리 |
| 메시지별 generated adapter | ROS 메시지 필드를 공유 메모리에 직접 연결 |
| CUDA Toolkit/driver와 호환되는 PyTorch | CUDA Tensor 사용 시 추가 필요 |

`hermos/`는 고정 revision을 가진 submodule입니다. 새 checkout에서는 사용자가
`git submodule update --init --recursive`로 준비합니다. heap hook은 backend CMake 빌드에서
함께 설치하며, 제공되는 `Cargo.lock`을 사용합니다. 첫 Cargo 빌드에는 해당 registry
의존성을 받을 네트워크 또는 준비된 cache가 필요합니다. GPU core의 고정된 원본과 패치
준비는 아래 GPU 빌드 문서를 따릅니다.

## CPU 패키지 빌드와 host 검증

ROS 2와 위 빌드 의존성이 준비된 워크스페이스 루트에서 실행합니다.

```bash
CMAKE_BUILD_PARALLEL_LEVEL=2 PYTHONDONTWRITEBYTECODE=1 bash scripts/verify
source /opt/ros/humble/setup.bash
source install/hermos/verify/local_setup.bash
```

빌드·설치·로그는 각각 `build/hermos/verify`, `install/hermos/verify`, `log/hermos/verify`에
생깁니다. 이 명령은 CPU 패키지와 host 테스트를 실행하며, 커널 모듈 설치·적재나
실장치 전송까지 준비해 주는 명령은 아닙니다. 장치가 필요한 테스트는 기본적으로 건너뜁니다.

실제 CPU 전송 프로세스에는 다음 조건이 추가로 필요합니다.

- 현재 소스와 일치하는 커널 모듈·공유 라이브러리·heap hook.
- 읽기/쓰기가 가능한 장치를 가리키는 `HERMOS_DEVICE_PATH`.
- 해당 **peer 프로세스의** `LD_PRELOAD`에 절대경로로 지정한 `libhermos_heaphook.so`와,
  같은 디렉터리의 일치하는 `libhermos.so`.
- `HERMOS_BRIDGE_MODE=off`, `HERMOS_NO_DISCOVERY_AGENT=1`.

실행 환경 준비와 재현 가능한 CPU 전송 시험은 [CPU 시나리오](../../experiments/g5/cpu_latency/README.md)를
참고하세요. 현재 이 시나리오의 CPU 등록 코드는 benchmark 전용이며, 범용 CPU supervisor
launcher를 제공하는 것은 아닙니다. [CPU 메시지 가이드](cpu-messages.md)의 코드는 준비된 프로세스에 넣는 애플리케이션 예제입니다.

## GPU 패키지 빌드

CPU 패키지 → repaired CUDA core와 메시지 패키지 → `rclpy_hermos_gpu` 순으로 빌드합니다.
정확한 명령은 [GPU 빌드 가이드](../../rclpy_hermos_gpu/README.md#build-and-verification)를 따르세요.
PyTorch는 사용하는 CUDA/장치에 맞게 별도로 준비해야 합니다.

GPU 실행에는 현재 소스와 일치하는 device bundle/manifest, 관리자 설치 bootstrap,
private-epoch supervisor가 필요합니다. [GPU 실행 계약](../../phase4_gpu_core_probe/repair/recovery/README.md)을
먼저 확인하세요. `source install/...`만으로 이 조건이 충족되지는 않습니다.
