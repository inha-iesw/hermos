# G4 Python GPU Tensor 통합 근거

2026-11-01. 대상은 Thor / CUDA 13.2.86 / driver 595.78 / kernel
6.8.12-1021-tegra / Ubuntu 22.04 container / ROS 2 Humble / Torch 2.13.0+cu132다.
G3의 **등록 direct peer·private IPC/mount namespace·process당 단일 epoch** 계약을 따른다.
일반 ROS launch, Orin/CUDA 12, native Jazzy, bridge 또는 성능 결과로 확대하지 않는다.

## 구현과 소유권

- 별도 `rclpy_agnocast_gpu` 패키지의 `_native` 하나만 repaired CUDA core static
  archive를 링크한다. `_types`는 기존 typed registry를 사용하는 CPU descriptor provider다.
  기존 CPU import 경로는 CUDA 초기화를 하지 않는다.
- `GpuRuntime.initialise()` → endpoint 생성 → `register()` → dispatch 순서를 적용한다.
  pool/mapper 설정은 process 공통이며, 같은 process에서 두 번째 epoch를 열지 않는다.
- `borrow_tensor(...).as_tensor()`는 core reservation의 pointer를 DLPack으로 내보낸다.
  writable slice/detach가 남으면 publish를 거부한다. core prepare 후에만 CPU descriptor를
  borrow/fill/publish하며, fill은 기존 typed worker에서 수행한다.
- consumer의 map과 ready wait는 실제 DLPack 요청 stream에 연결된다. native owner는
  마지막 Tensor alias가 사라진 뒤에도 해당 stream의 completion까지 유지된다.
  다른 stream에서 작업했다면 bound stream으로 완료 의존성을 연결해야 한다.
- 개별 endpoint close는 자신의 owner만 확인한다. 공유 mapper 봉인과 pool/context 종료는
  runtime이 담당한다. pending completion event는 owner 획득 전에 할당하고 마지막 alias
  해제 경로에서는 intrusive queue에 연결한다. event 오류는 owner를 보존한 채 poison으로 남긴다.
- 런처가 factory 뒤 signal handler를 다시 설치하여 Agnocast의 조기 native executor 종료를
  막는다. 기존 coroutine/client reply를 진행시키며 새 entity callback은 차단한다.
  peer drain 3초, supervisor TERM 5초다. poison·미완료 drain은 exit 70이며 정상 종료가 아니다.
- G3 canonical core patch는 이번 차수에서 변경하지 않았다. dense wire validator를 GPU
  package가 소유하고 G3 probe가 재사용한다. wire의 정렬된 nonzero offset 지원과 첫 Python
  facade의 offset-zero 제한을 구별한다.

## 검증 범위

초기 통합의 집계와 source/binary hash는 `log/g4/assessment.json`에 보존한다.
리뷰 후 구조 보완의 최신 근거는 `log/g4/refactor/assessment.json`과 아래 후속 절이다.
로그와 설치 산출물은
기존 ignored artifact 경로에 있으며 저장소 문서의 PASS를 재현 가능한 전체 배포물로
간주하지 않는다. 실패한 실행도 같은 `log/g4/` 아래에 보존했다.
아래 표와 copy trace 수치는 초기 §37의 기록이며, 구조 보완 이후 결과는 마지막 절을 따른다.

| 검증 | 결과·범위 |
|---|---|
| `bash scripts/verify` | CPU 12 packages / 39 targets, 470 PASS + 118 device opt-in SKIP |
| GPU host CTest | 5 targets, 80 PASS + 21 device opt-in SKIP |
| session runner host | 21 PASS; 기존 stock module의 존재/부재와 교체 검출 포함 |
| G3 descriptor 재사용 | 6 PASS; G3 transport peer도 새 public validator로 재빌드 |
| `device-full-02` | 28 PASS: 실제 장치 21건 + 분석기 host 7건 |
| 최신 통합·trace | `device-final-02`: 30 PASS(장치 21 + host 9), trace PASS, runner exit 0 |

실장치 21건은 다음을 각각 검증한다.

- native 8건: 지연 producer, alias/완료 전 재사용 금지, unpublished cancel, capsule 폐기,
  잘못된 layout, generation/fan-out, 다른 stream의 완료 연결, 실제 DLPack stream 불일치.
- public Tensor 전송 4건: Python→Python uint8/float32, Python→C++, C++→Python.
  Python consumer는 alias-held close 거부와 endpoint 2회 재생성도 확인한다.
- crash 2건: producer/consumer 중 하나를 pidfd로 SIGKILL한다. 남은 peer는 dead/retained
  owner 때문에 70으로 종료하고, 독립 그룹은 계속 전송하며 새 그룹도 정상 동작한다.
  supervisor가 살아남은 peer까지 SIGKILL하는 결과는 통과시키지 않는다.
- async 1건: `await` 중 CUDA alias를 보유한 reader가 TERM을 받은 뒤 기존 Future를 완료하고
  값을 확인·owner 반환·정상 종료한다.
- 실패 제어 2건: 실제 event-query 오류의 callback 없는 전파, 응답 없는 FD handoff 중 TERM.
  둘 다 supervisor의 SIGKILL 없이 exit 70과 전체 그룹 회수를 확인한다.
- DDS 4건: topic과 parameter service를 양방향으로 확인한다. Cyclone DDS는 기본 설정으로
  private epoch 내부/외부를 통과한다. Fast DDS는 내부 기본 설정, 외부 명시적 UDP profile을 쓴다.

Fast DDS **기본 설정의 cross-namespace 실패**는 `device-full-01`에 보존돼 있다.
`config/dds/fastdds_udp.xml`을 양쪽 participant에서 명시적으로 선택해야 한다.
profile source/install byte 일치와 실제 사용 경로·SHA256도 시험에서 기록한다.
GPU descriptor를 그룹 밖으로 보내거나 Agnocast bridge를 켠 결과가 아니다.

## Copy trace와 자원 관측

`test/integration/copy_trace/`는 설치된 trusted bootstrap으로 별도 진단 namespace를 만들고,
nsys가 production launcher 두 process를 추적한다. 이 진단은 등록 direct-peer supervisor
시험과 구별한다. payload는 uint8 64 bytes이며 값을 바꾸어 8회 전송한다.

Nsight Systems 2025.6.3의 실제 SQLite는 driver/runtime API를 `RUNTIME` table에 함께
기록한다. 분석기는 이 형식과 분리된 `DRIVER` 형식을 모두 시험한다. 두 peer PID,
publish/consume NVTX 각 8구간, API correlation과 memcpy activity를 함께 확인한다.

- 실제 trace의 memcpy 전체 8회는 checksum `.sum().item()` 결과를 읽는 8-byte DtoH다.
  전송 payload의 HtoD/DtoD 복사는 관측되지 않았다.
- VMM import 8회, create 1회, map/unmap/address reserve/free/release 각각 9회다.
  `device-final-02`에서 event create 63회 + IPC event open 8회에 대해 destroy 71회가 기록됐다.
  이들은 **API 호출·반환값 관측**이며 물리 VRAM 또는 live/high-water 계수로 해석하지 않는다.
- native 장치 시험은 같은 process 안에서 Tensor와 native reservation/map pointer의
  일치를 별도로 확인한다. 서로 다른 PID의 주소 숫자가 같다는 것으로 zero-copy를 판정하지 않는다.
- wrapper 통계는 active/pending/peak owner, wrapper event, configured pool bytes를 구별한다.
  endpoint 재생성 후 active/pending/event 0을 확인한다. core 내부 cache 전체를 측정한 값은 아니다.

이 장치의 nsys는 software-instrumented CUDA trace를 사용하며 Unified Memory tracing은
지원되지 않는다. coordinator는 CUDA/NVTX를 사용하지 않아 관련 경고가 남는다. peer의
실제 CUDA/NVTX 기록과 경고 PID는 분석 JSON에 보존한다. 성능 수치나 전체 VRAM 누수 판정은 없다.

## 실패 이력과 환경

- 장치 접근이 거부됐던 최초 실행 뒤 사용자 허용에 따라 container에 GID 993 `render`
  그룹을 등록하고 `ros`를 추가했다. device 파일 mode와 sudoers는 변경하지 않았다.
- 같은 process의 CUDA IPC event import를 시험하던 초기 fixture를 별도 source process로
  수정했다. 실제 transport는 별도 PID에서만 검증한다.
- CPU descriptor를 읽는 recursive closure가 root를 cyclic GC까지 보유하던 문제를
  module-level 재귀로 수정하고 GC-disabled host 회귀를 추가했다.
- signal handler 충돌, Fast DDS cross-namespace 기본 설정 실패와 실제 nsys schema 차이를
  각각 재현했다. `device-final-01`의 float32 실패는 stdout의 미완성 `RECEIVED` 조각을
  완성된 줄로 읽던 시험 도구 문제였다. 별도 RED→GREEN 회귀로 newline framing을 수정했다.
- 모든 장치 실행은 fresh stage와 canonical session runner를 사용했다. 본인 module의
  refcount 0 뒤 정상 unload, 기존 socket/metadata/module 보존, host `/dev/shm` 불변을 확인한다.
  driver 이름의 수동 unlink, CUDA reset 또는 unlink daemon kill을 수행하지 않았다.

## 재현과 남은 범위

[README](README.md)의 CPU adapter → repaired core/messages → G4 → G3 probe build를
순서대로 마친 뒤, 매번 새로운 절대 stage를 사용한다. C++ 상호운용 시험은 이 마지막
probe build의 `g3_transport_peer`를 실행하므로 G4 package만 빌드해서는 재현되지 않는다.

```bash
export PYTHONPATH="$PWD/phase4_gpu_core_probe/repair/recovery:$PWD/rclpy_agnocast_gpu/test/integration:$PYTHONPATH"
G3_RUN_TRANSPORT=1 G4_RUN_DEVICE=1 PYTHONDONTWRITEBYTECODE=1 \
  python3 phase4_gpu_core_probe/repair/session/run_device.py /absolute/new-stage \
  python3 -m pytest rclpy_agnocast_gpu/test/integration -vv \
  --junitxml=/absolute/new-stage/results.xml
```

Copy trace는 같은 runner의 command 자리에
`python3 rclpy_agnocast_gpu/test/integration/copy_trace/run.py /absolute/new-stage/trace`를 넣는다.
호스트 DDS 시험과 장치 session은 동시에 실행하지 않는다.

G5 성능·큰 payload/지속 부하·bridge·실데이터, core cache/FD/event의 live/high-water 확대 계측,
물리 VRAM 회수, Orin/CUDA 12와 native Jazzy는 남는다. consumer GPU mapping은 READWRITE이며
read-only 사용은 호출자 계약이다. stream을 drain 전에 파괴하거나 `record_stream()`만으로
외부 pool 수명이 해결된다고 가정하는 사용법은 지원하지 않는다.
이번 변경에 대한 finalize·commit-plan·Git mutation은 실행하지 않았다.

## 리뷰 후 구조 보완

`code-review-20261101-052858.md`의 리팩터링·설계 제안을 선별 반영했다.
MEDIUM 전체를 수정한 차수는 아니며 read 거부 정책, same-process pub/sub, registration
fail-open, supervisor CLI 종료 실패 전파와 미실측 지원 조합은 별도로 남긴다.

| 항목 | 반영·보류 판단 |
|---|---|
| R1 | native admit/release/poison 공통화. core 획득 전 할당과 event 파괴·owner 감소의 lock 순서 유지 |
| R2 | integration `support.py`에 peer 실행·Protocol·pidfd·inventory·종료 oracle 통합. 진단에서 pytest 간접 의존 제거 |
| R3 | CPU runtime open 검사와 시험 runtime 생성기를 재사용. 기존 validation 순서·오류 문구 유지 |
| R4 | `test/api/`의 5개 주제와 공통 fixture로 분리. 전후 56 testcase·34 test 함수·67 assertion AST 동일 |
| R5 | 보류. 오류 타입과 shape/device 검사 우선순위를 바꾸면서 줄이는 중복은 약 20줄 |
| D10 | 명시적 GPU 타입 판정·초기화 gate를 helper로 모음. 조건부 역의존은 남으며 provider manifest 확장은 유보 |
| D7 | supervisor의 source-tree-only 배포 경계와 독립 배포 전 결정 시점을 문서화. 코드 이동은 유보 |
| D9 | privileged entry·운영/진단 worker 통합은 recovery 계약을 함께 재검증하는 별도 차수로 유보 |

사용되지 않는 Loan/ReadExport device 필드·binding과 publisher handle 복제를 제거했다.
GPU `_make_message()`는 CPU base를 재사용하며, lock·예외·재전달 계약 시험 2건을 CPU gate에
추가했다. 빈 peer returncodes를 허용하던 공통 oracle은 RED→GREEN 회귀로 보완했다.
`validate_descriptor`는 metadata 입력 경계의 별도 시험 결정까지 유지한다.

G3 재현 문서의 빌드 순서를 core/messages → G4 → probe로 수정했고 비어 있는 CMake build
디렉터리에서 configure를 검증했다. 실제 probe 재빌드와 descriptor 6건도 통과했다.
canonical core patch, root-owned bootstrap 및 운영 supervisor 소스는 변경하지 않았다.

최신 검증 경로는 `log/g4/refactor/`다. CPU 전체 회귀는 **472 PASS + 118 opt-in SKIP**,
GPU host 5 targets는 **81 PASS + 21 opt-in SKIP**다. 장치 통합은 **31 PASS(실장치 21 +
host 보조 10)**, copy trace와 runner exit0도 통과했다. 장치 통합과 trace의 결과는
`device-01/results.xml`, `device-01/verification.json`, `device-01/trace/analysis.json`에 보존한다.
최종 hash·XML 집계는 `assessment.json`을 따른다. 이전 §37의 성공/실패 로그는 덮지 않았다.
