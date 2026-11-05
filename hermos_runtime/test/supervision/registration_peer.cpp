#include <iostream>
#include <string>

#include "hermos_runtime/supervision/registration.hpp"

int main()
{
  try {
    hermos_runtime::supervision::register_peer({});
    std::cout << "registered" << std::endl;
    std::string command;
    std::getline(std::cin, command);
    return command == "finish" ? 0 : 1;
  } catch (const std::exception & error) {
    std::cerr << error.what() << std::endl;
    return 70;
  }
}
