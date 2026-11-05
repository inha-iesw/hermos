# HERM0S 관리자 설치 계약

이 디렉터리는 설치할 **설정 원본**이다. colcon 설치는 이를 시스템에 적용하거나
module을 적재하지 않는다. 공개 apt 패키지가 아니며, DKMS·udev·부팅 관리자가 있는
호스트에서 관리자가 준비한다. 컨테이너 실행도 호스트 커널 준비가 먼저다.

v1은 DKMS 등록명과 모듈명 `hermos_rclpy`, 장치 `/dev/hermos_rclpy`를 사용한다.
기존 backend의 `hermos` 등록과 구분하며 다른 모듈을 제거하거나 교체하지 않는다.
CPU wrapper에는 sudo·module 적재 권한이 필요하지 않다.

## 관리자 준비

1. 검증한 backend source snapshot의 `hermos_kmod/`를 root 소유의
   `/usr/src/hermos_rclpy-2.3.5/`에 복사하고, 그 안의 `dkms.conf`를 이 디렉터리의
   설정으로 교체한다. 전체 디렉터리와 상위 경로는 일반 사용자가 수정할 수 없어야 한다.
   기존 같은 이름·버전의 등록이 있으면 덮어쓰지 말고 source identity를 먼저 대조한다.
2. 대상 커널의 headers와 DKMS를 설치한다. 다음은 명령 형식이며 `<target-kernel>`은
   실제 대상 버전으로 바꾼다. 실행 중 커널과 다른 대상을 지정할 수 있다.

   ```bash
   sudo dkms add -m hermos_rclpy -v 2.3.5
   sudo dkms build -m hermos_rclpy -v 2.3.5 -k '<target-kernel>'
   sudo dkms install -m hermos_rclpy -v 2.3.5 -k '<target-kernel>'
   ```

3. 시스템 그룹 `hermos`를 준비하고 허용할 사용자만 가입시킨다. 새 그룹은 로그인
   세션을 갱신한 뒤 적용된다. `70-hermos-rclpy.rules`를 root:root, 0644로
   `/etc/udev/rules.d/`에 설치하고 udev 규칙을 다시 읽힌다. 새 장치는 root:hermos,
   0660이어야 한다. backend 기본 모듈 `hermos`의 기존 0666 정책은 바꾸지 않는다.
4. 실행 중인 HERM0S peer가 없는 유지보수 시점에 `sudo modprobe hermos_rclpy`로
   모듈을 적재한다. 이미 적재된 다른 버전의 교체·강제 unload는 이 절차에 포함하지 않는다.
   실행 중 커널과 다른 대상에 설치했다면 해당 커널로 부팅하기 전에는 적재하지 않는다.
5. 부팅 시 적재가 필요하면 `hermos-rclpy.conf`를 root:root, 0644로
   `/etc/modules-load.d/`에 설치한다. DKMS의 `AUTOINSTALL`은 커널별 빌드/설치를
   뜻하며 부팅 시 적재를 대신하지 않는다. GPU bootstrap의 관리자 소유 경로와
   권한은 이 CPU 설정으로 변경하지 않는다.

## 설치 수용 확인

`modinfo -F name hermos_rclpy`, `modinfo -F vermagic hermos_rclpy`,
`/sys/module/hermos_rclpy/srcversion`을 설치 artifact와 대조한다. 일반 사용자 세션에서
장치가 character device이고 root:hermos·0660이며 읽기/쓰기 가능한지 확인한다.
부팅 후에도 이 조건과 정상 CPU 전달·종료를 다시 확인하고, 기존 다른 모듈이 보존되는지
확인한다. 소스/라이브러리/heap hook/provider를 함께 식별한 실행 기록을 남긴다.

현재 컨테이너의 unit/build 시험은 실제 DKMS 설치, 다른 커널로의 부팅, udev 적용,
재부팅 후 적재를 대신하지 않는다. 배포된 커널·사용자 권한 조합별로 이 검증이 필요하다.

## GPU namespace bootstrap

`namespace-bootstrap.py`는 별도로 검토·설치할 관리자 원본이다. 운영 경로는
`/usr/local/libexec/rclpy-zc-gpu/namespace-bootstrap.py`이며 파일과 모든 상위 경로는
root 소유이고 일반 사용자가 수정할 수 없어야 한다. colcon은 이 경로를 변경하지 않는다.
설치 supervisor가 이 원본과 운영 파일의 bytes를 비교한 뒤 고정 경로만 호출한다.
bootstrap은 표준 라이브러리만 사용하고, UID/GID·capability를 영구히 낮춘 뒤
설치된 비특권 worker를 실행한다. 최소 sudo 규칙과 설치 절차는
[recovery 운영 계약](../../../phase4_gpu_core_probe/repair/recovery/README.md#administrator-installation)을 따른다.
