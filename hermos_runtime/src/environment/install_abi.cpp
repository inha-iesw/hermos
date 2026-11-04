// SPDX-License-Identifier: Apache-2.0
#include <fstream>
#include "rclpy_hermos/types/adapter.hpp"

int main(int argc, char ** argv)
{
  if (argc != 2) {
    return 1;
  }
  std::ofstream output(argv[1]);
  output << rclpy_hermos_adapter_abi_version() << '\n' << rclpy_hermos_adapter_build_abi();
  return output.good() ? 0 : 1;
}
