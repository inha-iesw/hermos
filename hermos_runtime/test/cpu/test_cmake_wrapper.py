"""Generated wrappers must be executable even when ament installs symlinks."""

import os
from pathlib import Path
import subprocess


def test_wrapper_modes_and_private_native_install(tmp_path):
    helper = Path(__file__).resolve().parents[2] / 'cmake/hermos_install_cpu_executable.cmake'
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'main.cpp').write_text('int main() { return 0; }\n')
    (source / 'CMakeLists.txt').write_text(f'''
cmake_minimum_required(VERSION 3.20)
project(wrapper_fixture)
include("{helper}")
hermos_install_cpu_executable(python_peer PYTHON_ENTRY example:main)
add_executable(actual_peer main.cpp)
hermos_install_cpu_executable(cpp_peer TARGET actual_peer)
''')
    build = tmp_path / 'build'
    prefix = tmp_path / 'prefix'
    subprocess.run(['cmake', '-S', str(source), '-B', str(build),
                    f'-DCMAKE_INSTALL_PREFIX={prefix}'], check=True, capture_output=True)
    # This is the source of the installed symlink, not only a copied PROGRAM.
    assert os.access(build / 'cpu-wrappers/python_peer', os.X_OK)
    assert os.access(build / 'cpu-wrappers/cpp_peer', os.X_OK)
    subprocess.run(['cmake', '--build', str(build), '--target', 'install'],
                   check=True, capture_output=True)
    executables = {path.name for path in (prefix / 'lib/wrapper_fixture').rglob('*')
                   if path.is_file() and os.access(path, os.X_OK)}
    assert executables == {'python_peer', 'cpp_peer'}
    assert (prefix / 'libexec/wrapper_fixture/actual_peer').is_file()
