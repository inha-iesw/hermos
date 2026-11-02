# 사용자 정의 메시지 연결

[프로젝트 README](../../README.md) · [CPU 메시지](cpu-messages.md)

`uint64 sequence`와 `uint8[] data` 같은 특정 구조에 한정되지 않습니다. scalar, 고정 배열,
bounded/unbounded sequence, 문자열·wstring, 중첩 메시지를 처리하는 adapter를 생성할 수 있습니다.
타입을 지원하는 것과 모든 필드를 연속 버퍼로 내보낼 수 있는 것은 별개입니다.
예를 들어 packed boolean sequence는 `buffer()`를 지원하지 않습니다.
서비스·action·재귀 메시지는 이 메시지 adapter 생성 대상이 아닙니다.

메시지 패키지 `my_robot_msgs`를 빌드·설치한 뒤, 별도 `ament_cmake` 패키지
`my_robot_adapters`에서 해당 타입의 provider를 생성합니다. Python 패키지 초기화를 위한
`my_robot_adapters/__init__.py`도 준비합니다. CMake 구성 예시는 다음과 같습니다.

```cmake
cmake_minimum_required(VERSION 3.14)
project(my_robot_adapters)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy_hermos_codegen REQUIRED)

ament_python_install_package(${PROJECT_NAME})
rclpy_hermos_generate_adapters(_types TYPES
  my_robot_msgs/msg/Frame
)
ament_package()
```

`package.xml`에는 기본 패키지 메타데이터와 함께 다음 의존성을 포함합니다.

```xml
<buildtool_depend>ament_cmake</buildtool_depend>
<buildtool_depend>ament_cmake_python</buildtool_depend>
<buildtool_depend>rclpy_hermos_codegen</buildtool_depend>
<build_depend>pybind11_vendor</build_depend>
<build_depend>python3-dev</build_depend>
<depend>rclpy_hermos</depend>
<depend>my_robot_msgs</depend>
<export><build_type>ament_cmake</build_type></export>
```

빌드된 adapter overlay를 source하고, 애플리케이션 패키지의 실행 의존성에
`my_robot_adapters`를 포함하세요. provider는 ament index에서 발견되므로 `_types`를
직접 import할 필요가 없습니다. 실제 구성은 [기본 adapter 패키지](../../rclpy_hermos_adapters/)와
[생성 함수](../../rclpy_hermos_codegen/cmake/rclpy_hermos_generate_adapters.cmake)를 참고할 수 있습니다.
