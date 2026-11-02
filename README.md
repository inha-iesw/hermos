# HERM0S

**Heterogeneous ROS 2 Messaging with Zero-Copy Support**

HERM0S(헤르모스)는 동일 호스트의 ROS 2 프로세스 사이에서 **CPU 메시지와 CUDA Tensor를
복사 없이 공유**하기 위한 프레임워크입니다. Python/rclpy와 C++ 노드가 함께 동작하는
센서·영상·추론 파이프라인의 직렬화와 payload 복사 비용을 줄이는 것을 목표로 합니다.

표시명은 **HERM0S**(숫자 `0`), 코드·패키지·경로는 **`hermos`**(영문자 `o`)를 사용합니다.
CPU→GPU 업로드나 기존 배열을 공유 버퍼에 옮기는 복사까지 자동으로 없어지지는 않습니다.

## 빠른 시작

ROS 2 Humble과 빌드 의존성, 별도 저장소인 `hermos/` backend 소스를 준비한 뒤
워크스페이스 루트에서 실행합니다. 자세한 조건은 [환경 준비와 빌드](guides/developer/setup.md)를 참고하세요.

```bash
CMAKE_BUILD_PARALLEL_LEVEL=2 PYTHONDONTWRITEBYTECODE=1 bash scripts/verify
source /opt/ros/humble/setup.bash
source install/hermos/verify/local_setup.bash
```

이 명령은 CPU 패키지 빌드와 host 검증을 수행합니다. 실제 전송에는 커널 모듈·공유 힙
실행 환경이 추가로 필요하고, GPU는 별도 빌드와 supervisor 설정이 필요합니다.

## 개발자 가이드

| 문서 | 내용 |
|---|---|
| [환경 준비와 빌드](guides/developer/setup.md) | 의존성, 지원 범위, CPU/GPU 실행 조건 |
| [CPU 메시지](guides/developer/cpu-messages.md) | 일반 rclpy와의 차이, Image 발행·구독, 가변 길이·한도·수명, CPU Torch |
| [CUDA Tensor](guides/developer/gpu-tensors.md) | PyTorch 예제, GPU peer 실행, stream·버퍼 수명 |
| [사용자 정의 메시지](guides/developer/custom-messages.md) | 메시지 타입별 adapter 생성·연결 |
| [문제 해결](guides/developer/troubleshooting.md) | preload, QoS, proxy, publish·종료 오류 |

현재는 소스 빌드와 실행 환경 준비가 필요한 개발 단계입니다. 실장치 근거는
Thor의 Ubuntu 22.04 / ROS 2 Humble 컨테이너 환경을 중심으로 축적되어 있습니다.
GPU는 같은 supervisor에 등록된 별도 프로세스 구성이 기준이며, 일반 `ros2 launch`
배포나 임의의 플랫폼 조합이 검증된 상태는 아닙니다.

## 성능과 검증

[CPU](experiments/g5/cpu_latency/README.md)·[GPU](experiments/g5/gpu_latency/README.md)
측정 시나리오, [측정 결과와 한계](experiments/g5/EVIDENCE.md),
[성능 개선 계획](experiments/g5/PERFORMANCE_PLAN.md)을 제공합니다.
zero-copy 지원만으로 DDS보다 빠르다고 단정할 수는 없으며, 결과는 실행 조건과 함께 해석해야 합니다.

## 라이선스와 외부 코드 사용

CPU backend에는 Agnocast의 공유 힙 통신·C++ runtime·heap hook·커널 모듈을 수정한 코드가
포함되어 있습니다. 사용한 구성 요소와 원본 라이선스는 다음과 같습니다.

| 구성 요소 | 사용 위치 | 라이선스 |
|---|---|---|
| Agnocast userspace runtime / heap hook | `hermos/src/hermoslib/`, `hermos/hermos_heaphook/` | [Apache-2.0](hermos/LICENSE) |
| Agnocast 커널 모듈 | `hermos/hermos_kmod/` | [GPL-2.0-only OR BSD-2-Clause](hermos/hermos_kmod/hermos_main.c) |
| ros2_cuda_ipc | CUDA IPC core와 descriptor 메시지; [고정 원본 정보](phase4_gpu_core_probe/upstream.lock.json) | [MIT](phase4_gpu_core_probe/repair/test/upstream/LICENSE) |
| DLPack | CPU/GPU Tensor 교환용 헤더 | [Apache-2.0](rclpy_zc/third_party/dlpack/LICENSE) |

원본 라이선스·저작권 고지·출처는 해당 소스와 함께 보존합니다.
다른 의존성에는 각각의 라이선스가 적용됩니다.
