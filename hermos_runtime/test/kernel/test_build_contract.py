"""Kernel target forwarding without loading a module or requiring root."""

from pathlib import Path
import shutil
import subprocess

import pytest


WORKSPACE = Path(__file__).resolve().parents[3]
KMOD = WORKSPACE / 'hermos/hermos_kmod'
SYSTEM = WORKSPACE / 'hermos_runtime/packaging/system'


@pytest.mark.parametrize('makefile,arguments,expected_mode', [
    (KMOD / 'Makefile', [], '0666'),
    (KMOD / 'Makefile', ['HERMOS_MODULE_NAME=hermos_rclpy'], '0660'),
    (KMOD / 'Makefile', ['KUNIT_BUILD=y', 'CONFIG_KUNIT=y', 'CONFIG_GCOV_KERNEL=y'], '0666'),
    (WORKSPACE / 'hermos/.github/kunit/Kbuild', ['CONFIG_KUNIT=y'], '0666'),
])
def test_build_entrypoints_compile_their_device_policy(
        tmp_path, makefile, arguments, expected_mode):
    # Evaluate the real Make flags and C constant, including the alternate CI
    # Makefile that does not include the ordinary module Makefile.
    (tmp_path / 'policy.c').write_text(
        '#ifndef HERMOS_DEVICE_MODE\n#error device policy missing\n#endif\n'
        f'_Static_assert(HERMOS_DEVICE_MODE == {expected_mode}, "device policy");\n')
    harness = tmp_path / 'probe.mk'
    harness.write_text('policy:\n\t$(CC) $(ccflags-y) -c policy.c -o policy.o\n')
    result = subprocess.run(
        ['make', '-f', str(makefile), '-f', str(harness), f'src={KMOD}',
         *arguments, 'policy'], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / 'policy.o').is_file()


@pytest.mark.parametrize('target', ['all', 'test', 'sparse', 'clean'])
@pytest.mark.parametrize('version_argument', ['KVERSION', 'KERNEL_VERSION'])
def test_selected_kernel_and_module_directory_reach_recursive_make(
        tmp_path, target, version_argument):
    module = tmp_path / 'module'
    module.mkdir()
    shutil.copy2(KMOD / 'Makefile', module / 'Makefile')
    kernel = tmp_path / 'kernel'
    kernel.mkdir()
    observed = tmp_path / 'observed'
    (kernel / 'Makefile').write_text(
        '.PHONY: modules clean\n'
        'modules clean:\n'
        f'\t@printf "%s\\n" "$(M)" "$(KERNEL_VERSION)" '
        f'"$(HERMOS_MODULE_NAME)" "$(KUNIT_BUILD)" "$(C)" "$@" > {observed}\n')
    result = subprocess.run(
        ['make', '-C', str(module), target, f'{version_argument}=5.15.999-test',
         f'KERNEL_BUILD_DIR={kernel}', 'HERMOS_MODULE_NAME=hermos_rclpy'],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed.read_text().splitlines() == [
        str(module), '5.15.999-test', 'hermos_rclpy',
        'y' if target == 'test' else '', '2' if target == 'sparse' else '',
        'clean' if target == 'clean' else 'modules']


def test_default_kernel_build_path_uses_requested_version(tmp_path):
    # -n still enters recursive make; the deliberately missing tree must be
    # the selected target, never the running kernel's header directory.
    result = subprocess.run(
        ['make', '-n', '-C', str(KMOD), 'KVERSION=0.0.0-hermos-test'],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert '/lib/modules/0.0.0-hermos-test/build' in result.stdout + result.stderr


@pytest.mark.parametrize('configuration,package,module', [
    (KMOD / 'dkms.conf', 'hermos', 'hermos'),
    (SYSTEM / 'dkms.conf', 'hermos_rclpy', 'hermos_rclpy'),
])
def test_dkms_configuration_preserves_target_and_registry(
        configuration, package, module):
    result = subprocess.run(
        ['bash', '-c', 'kernelver=6.9.999-test; source "$1"; '
         'printf "%s\\n" "$PACKAGE_NAME" "${BUILT_MODULE_NAME[0]}" '
         '"${MAKE[0]}" "$CLEAN"', 'dkms-contract', str(configuration)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    registry, built, build, clean = result.stdout.splitlines()
    assert (registry, built) == (package, module)
    assert 'KERNEL_VERSION=6.9.999-test' in build
    assert 'KERNEL_VERSION=6.9.999-test' in clean
    if module == 'hermos_rclpy':
        assert 'HERMOS_MODULE_NAME=hermos_rclpy' in build
        assert 'HERMOS_MODULE_NAME=hermos_rclpy' in clean
