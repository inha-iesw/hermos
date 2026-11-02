#include <cuda.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/wait.h>
#include <chrono>
#include <thread>

#include <cstdlib>
#include <iostream>
#include <string>

namespace
{
void check(const char * operation, CUresult result)
{
  std::cout << operation << " " << result << std::endl;
  if (result != CUDA_SUCCESS) {std::exit(1);}
}
}  // namespace

// Standalone driver diagnostic: no Hermos, CUDART, core patch, or GPU reset.
// The parent records shared-memory inventories without deleting driver names.
int main(int argc, char ** argv)
{
  const std::string mode = argc > 1 ? argv[1] : "allocate";
  const bool importing = mode == "import" || mode == "import_exit";
  const bool export_fd = mode == "export" || mode == "pair" || mode == "crash_pair";
  std::cout << "pid " << getpid() << " mode " << mode << std::endl;
  CUcontext context = nullptr;
  check("cuInit", cuInit(0));
  check("cuDevicePrimaryCtxRetain", cuDevicePrimaryCtxRetain(&context, 0));
  check("cuCtxPushCurrent", cuCtxPushCurrent(context));
  CUmemAllocationProp property{};
  property.type = CU_MEM_ALLOCATION_TYPE_PINNED;
  property.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
  property.location.id = 0;
  property.requestedHandleTypes = CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR;
  std::size_t bytes = 0;
  check("cuMemGetAllocationGranularity", cuMemGetAllocationGranularity(
      &bytes, &property, CU_MEM_ALLOC_GRANULARITY_MINIMUM));
  CUdeviceptr pointer = 0;
  CUmemGenericAllocationHandle allocation = 0;
  check("cuMemAddressReserve", cuMemAddressReserve(&pointer, bytes, 0, 0, 0));
  if (importing) {
    if (argc != 3) {return 4;}
    const int descriptor = std::stoi(argv[2]);
    check("cuMemImportFromShareableHandle", cuMemImportFromShareableHandle(
        &allocation, reinterpret_cast<void *>(static_cast<intptr_t>(descriptor)),
        CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR));
    if (close(descriptor) != 0) {return 2;}
  } else {
    check("cuMemCreate", cuMemCreate(&allocation, bytes, &property, 0));
  }
  check("cuMemMap", cuMemMap(pointer, bytes, 0, allocation, 0));
  CUmemAccessDesc access{};
  access.location = property.location;
  access.flags = CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
  check("cuMemSetAccess", cuMemSetAccess(pointer, bytes, &access, 1));
  if (mode == "import_exit") {_exit(0);}
  if (export_fd) {
    int descriptor = -1;
    check("cuMemExportToShareableHandle", cuMemExportToShareableHandle(
        &descriptor, allocation, CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR, 0));
    if (mode == "pair" || mode == "crash_pair") {
      if (fcntl(descriptor, F_SETFD, 0) != 0) {return 5;}
      const std::string number = std::to_string(descriptor);
      const char * child_mode = mode == "pair" ? "import" : "import_exit";
      const pid_t child = fork();
      if (child < 0) {return 6;}
      if (child == 0) {
        // No CUDA or allocation between fork and exec: the importer starts fresh.
        execl(argv[0], argv[0], child_mode, number.c_str(), nullptr);
        _exit(127);
      }
      int status = 0;
      const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
      while (waitpid(child, &status, WNOHANG) == 0) {
        if (std::chrono::steady_clock::now() >= deadline) {
          kill(child, SIGKILL);
          waitpid(child, &status, 0);
          return 7;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
      }
      if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {return 8;}
    }
    if (close(descriptor) != 0) {return 2;}
    std::cout << "close_export_fd 0" << std::endl;
  }
  check("cuMemUnmap", cuMemUnmap(pointer, bytes));
  check("cuMemAddressFree", cuMemAddressFree(pointer, bytes));
  check("cuMemRelease", cuMemRelease(allocation));
  CUcontext popped = nullptr;
  check("cuCtxPopCurrent", cuCtxPopCurrent(&popped));
  if (popped != context) {return 3;}
  check("cuDevicePrimaryCtxRelease", cuDevicePrimaryCtxRelease(0));
  return 0;
}
