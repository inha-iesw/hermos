# 문제 해결

[프로젝트 README](../../README.md) · [환경 준비](setup.md) · [CPU 메시지](cpu-messages.md) · [CUDA Tensor](gpu-tensors.md)

| 증상 | 확인할 사항 |
|---|---|
| `No module named rclpy_hermos` | 새 패키지를 빌드하고 올바른 `local_setup.bash`를 source했는지 확인 |
| 메시지 adapter를 찾지 못함 / ABI 불일치 | 해당 타입의 provider 설치·overlay와 runtime/header 버전 일치 여부 확인 |
| runtime 생성 시 preload/device 오류 | peer의 heap hook 절대경로, 일치하는 라이브러리, 장치 접근 권한 확인 |
| QoS 거부 | 정수 depth 대신 명시적 BEST_EFFORT QoSProfile 사용 |
| resize/만료된 proxy 오류 | 첫 쓰기 전에 모든 resize 완료, resize 뒤 root에서 하위 필드 재획득 |
| publish 시 writable export 오류 | memoryview/Tensor와 파생 alias를 발행 전에 모두 해제 |
| 종료 시 busy / drain timeout | 보유한 메시지·Tensor·진행 중 callback과 stream 작업 확인 |

공유 자원이 남은 상태에서 강제로 메모리를 회수하거나, 전역 공유 메모리를 삭제하거나,
실행 중인 GPU pool을 초기화하지 마세요. 정상 종료가 불가능한 GPU 그룹의 복구는 supervisor의
종료·재시작 계약을 따릅니다.
