#pragma once

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <unistd.h>

namespace hermos_runtime::supervision
{
/**
 * @brief Register a complete initialised peer epoch before dispatch starts.
 * @param paths All owned metadata/socket paths; empty for a consumer.
 * @throws std::runtime_error if registration or the supervisor ACK fails.
 * @note Standalone peers omit both environment variables. A supervised peer
 * must not replace its pool or launch additional GPU holders after registration.
 * Startup before this snapshot is intentionally not an automatic recovery scope.
 */
inline void register_peer(const std::vector<std::string> & paths)
{
  const char * manifest = std::getenv("G3_RECOVERY_MANIFEST");
  const char * acknowledgement = std::getenv("G3_RECOVERY_ACK");
  if (!manifest && !acknowledgement) {return;}
  if (!manifest || !acknowledgement || !std::filesystem::path(manifest).is_absolute() ||
    !std::filesystem::path(acknowledgement).is_absolute())
  {
    throw std::runtime_error("Invalid recovery registration paths");
  }
  const std::string temporary = std::string(manifest) + ".tmp";
  if (std::filesystem::exists(manifest) || std::filesystem::exists(temporary) ||
    std::filesystem::exists(acknowledgement))
  {
    throw std::runtime_error("Recovery registration must use a new epoch");
  }
  std::ofstream output(temporary);
  output << "{\"version\":1,\"pid\":" << getpid() << ",\"resources\":[";
  for (std::size_t index = 0; index < paths.size(); ++index) {
    if (index) {output << ',';}
    // Core paths consist only of fixed prefixes, numeric IDs and UUIDs.
    output << std::quoted(paths[index]);
  }
  output << "]}\n";
  output.close();
  if (!output) {throw std::runtime_error("Recovery manifest write failed");}
  std::filesystem::rename(temporary, manifest);
  const char * timeout_value = std::getenv("G3_RECOVERY_START_TIMEOUT");
  const double timeout = timeout_value ? std::stod(timeout_value) : 10.0;
  if (!(timeout > 0)) {throw std::runtime_error("Invalid recovery startup timeout");}
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::duration<double>(timeout);
  while (!std::filesystem::exists(acknowledgement)) {
    if (std::chrono::steady_clock::now() >= deadline) {
      throw std::runtime_error("Recovery registration ACK timed out");
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
}
}  // namespace hermos_runtime::supervision
