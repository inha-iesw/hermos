// Copyright 2026 rclpy zerocopy maintainers
// SPDX-License-Identifier: Apache-2.0

#include <fstream>
#include "rclpy_hermos/types/adapter.hpp"

int main(int argc, char ** argv)
{
  if (argc != 2) {
    return 1;
  }
  std::ofstream output(argv[1]);
  output << rclpy_hermos::types::compiled_abi_tag();
  return output.good() ? 0 : 1;
}
