#include <dirent.h>
#include <fcntl.h>
#include <gtest/gtest.h>
#include <sys/stat.h>
#include <unistd.h>
#include <uuid/uuid.h>

#include <memory>
#include <fstream>
#include <string>
#include <utility>

#include "../support.hpp"
#include "producer_failure.hpp"
#include "ros2_cuda_ipc_core/publisher/gpu_buffer_manager.hpp"

namespace
{
using g3_cleanup::Operation;
using Manager = ros2_cuda_ipc_core::publisher::GpuBufferManager;

int open_fd_count()
{
  DIR * directory = opendir("/proc/self/fd");
  if (!directory) {return -1;}
  int count = 0;
  while (const auto * entry = readdir(directory)) {
    if (entry->d_name[0] != '.') {++count;}
  }
  closedir(directory);
  return count;
}

void expect_current_context(CUcontext expected)
{
  CUcontext current = nullptr;
  ASSERT_EQ(cuCtxGetCurrent(&current), CUDA_SUCCESS);
  EXPECT_EQ(current, expected);
}

struct FailureCase
{
  const char * name;
  Operation operation;
  unsigned sockets = 1;
  unsigned occurrence = 2;
  const char * diagnostic = nullptr;
};

class InitialisationFixture : public ::testing::Test
{
protected:
  void SetUp() override
  {
    ASSERT_EQ(::testing::UnitTest::GetInstance()->test_to_run_count(), 1);
    ASSERT_EQ(cudaSetDevice(0), cudaSuccess);
    stream_ = std::make_unique<g3_test::Stream>();
    // Warm CUDA and its driver FDs before measuring this pool's rollback.
    ASSERT_TRUE(manager_.initialise());
    ASSERT_TRUE(manager_.try_close());
    g3_cleanup::record_owned_names();
    before_ = g3_cleanup::counts();
    descriptors_ = open_fd_count();
    ASSERT_GT(descriptors_, 0);
    ASSERT_EQ(cuCtxGetCurrent(&previous_context_), CUDA_SUCCESS);
  }

  void TearDown() override {EXPECT_TRUE(g3_cleanup::unlink_owned_names());}

  void expect_failed_start(
    Operation operation, unsigned metadata, unsigned sockets, const char * diagnostic = nullptr)
  {
    if (diagnostic) {::testing::internal::CaptureStderr();}
    EXPECT_FALSE(manager_.initialise());
    if (diagnostic) {
      const auto log = ::testing::internal::GetCapturedStderr();
      const auto found = log.find(diagnostic);
      EXPECT_NE(found, std::string::npos) << log;
      if (found != std::string::npos) {
        EXPECT_EQ(log.find(diagnostic, found + std::string(diagnostic).size()), std::string::npos)
          << log;
      }
    }
    EXPECT_FALSE(manager_.is_initialised());
    EXPECT_FALSE(manager_.poisoned());
    EXPECT_EQ(g3_cleanup::injections(operation), 1u);
    EXPECT_EQ(g3_cleanup::owned_metadata_count(), metadata);
    EXPECT_EQ(g3_cleanup::owned_socket_count(), sockets);
    expect_baseline();
    after_ = g3_cleanup::counts();
  }

  unsigned delta(Operation operation) const
  {
    return g3_cleanup::count(after_, operation) - g3_cleanup::count(before_, operation);
  }

  void expect_thrown_start(Operation operation, unsigned sockets)
  {
    ::testing::internal::CaptureStderr();
    EXPECT_THROW(manager_.initialise(), std::bad_alloc);
    const auto log = ::testing::internal::GetCapturedStderr();
    const std::string diagnostic = "Pool initialisation threw; rolling back owned resources";
    const auto found = log.find(diagnostic);
    EXPECT_NE(found, std::string::npos) << log;
    if (found != std::string::npos) {
      EXPECT_EQ(log.find(diagnostic, found + diagnostic.size()), std::string::npos) << log;
    }
    EXPECT_FALSE(manager_.is_initialised());
    EXPECT_FALSE(manager_.poisoned());
    EXPECT_EQ(g3_cleanup::injections(operation), 1u);
    EXPECT_EQ(g3_cleanup::owned_metadata_count(), 2u);
    EXPECT_EQ(g3_cleanup::owned_socket_count(), sockets);
    expect_baseline();
    after_ = g3_cleanup::counts();
  }

  void expect_balanced_resources()
  {
    // Successful allocations must each have a matching successful release.
    for (const auto & pair : {
        std::pair{Operation::MemAddressReserve, Operation::MemAddressFree},
        std::pair{Operation::MemCreate, Operation::MemRelease},
        std::pair{Operation::MemMap, Operation::MemUnmap},
        std::pair{Operation::EventCreate, Operation::EventDestroy},
        std::pair{Operation::ContextPush, Operation::ContextPop},
        std::pair{Operation::PrimaryRetain, Operation::PrimaryRelease}})
    {
      EXPECT_EQ(delta(pair.first) - g3_cleanup::injections(pair.first), delta(pair.second))
        << static_cast<unsigned>(pair.first);
    }
  }

  void expect_restart(Operation operation)
  {
    // A healthy failed start must release the epoch as well as names/FDs.
    Manager replacement({2, 64, 0});
    ASSERT_TRUE(replacement.initialise());
    EXPECT_TRUE(g3_cleanup::owned_fds_cloexec());
    auto first = replacement.acquire_for_publish(stream_->driver());
    auto second = replacement.acquire_for_publish(stream_->driver());
    ASSERT_TRUE(first);
    ASSERT_TRUE(second);
    EXPECT_NE(first->device_ptr(), second->device_ptr());
    ASSERT_TRUE(first->prepare_publish(stream_->driver()));
    ASSERT_TRUE(second->prepare_publish(stream_->driver()));
    ASSERT_EQ(cudaStreamSynchronize(stream_->value), cudaSuccess);
    EXPECT_TRUE(replacement.try_close());
    // Completed PublishBlocks still own their reservation's metadata mapping.
    // Release those aliases before measuring the fully settled map baseline.
    first.reset();
    second.reset();
    ASSERT_TRUE(manager_.initialise());
    EXPECT_TRUE(manager_.try_close());
    EXPECT_FALSE(manager_.poisoned());
    EXPECT_EQ(g3_cleanup::injections(operation), 1u);
    expect_baseline();
  }

  void expect_fallback_start()
  {
    ASSERT_TRUE(manager_.initialise());
    EXPECT_TRUE(g3_cleanup::owned_fds_cloexec());
    EXPECT_EQ(g3_cleanup::owned_socket_count(), 2u);
    EXPECT_EQ(g3_cleanup::injections(Operation::SocketFallback), 1u);
    EXPECT_TRUE(manager_.try_close());
    expect_baseline();
  }

private:
  void expect_baseline()
  {
    EXPECT_TRUE(g3_cleanup::owned_names_absent());
    EXPECT_EQ(open_fd_count(), descriptors_);
    const auto now = g3_cleanup::counts();
    EXPECT_EQ(g3_cleanup::count(now, Operation::MetadataMap) -
      g3_cleanup::injections(Operation::MetadataMap),
      g3_cleanup::count(now, Operation::MetadataUnmap));
    // This checks top-of-stack identity; push/pop counts detect same-context leaks.
    expect_current_context(previous_context_);
  }

  // The manager must be destroyed while its stream is still valid.
  std::unique_ptr<g3_test::Stream> stream_;
  Manager manager_{{2, 64, 0}};
  g3_cleanup::Counts before_{};
  g3_cleanup::Counts after_{};
  int descriptors_ = 0;
  CUcontext previous_context_ = nullptr;
};

class ProducerInitialisation :
  public InitialisationFixture, public ::testing::WithParamInterface<FailureCase> {};

TEST_P(ProducerInitialisation, SecondBlockFailureRollsBackAndAllowsRetry)
{
  const auto & failure = GetParam();
  const auto operation = failure.operation;
  g3_cleanup::arm(operation, failure.occurrence, false, false);
  expect_failed_start(operation, 2, failure.sockets, failure.diagnostic);
  if (operation == Operation::ContextPush) {
    // Rollback also needs context activation. One ready event existed before
    // the second event's activation failed.
    EXPECT_GE(delta(operation), failure.occurrence);
    EXPECT_EQ(delta(Operation::EventCreate), 1u);
    EXPECT_EQ(delta(Operation::IpcGetEventHandle), 1u);
  } else {
    EXPECT_EQ(delta(operation), failure.occurrence);
  }
  expect_balanced_resources();
  expect_restart(operation);
}

INSTANTIATE_TEST_SUITE_P(
  Allocation, ProducerInitialisation,
  ::testing::Values(
    FailureCase{"MemAddressReserve", Operation::MemAddressReserve},
    FailureCase{"MemCreate", Operation::MemCreate},
    FailureCase{"MemMap", Operation::MemMap},
    FailureCase{"MemSetAccess", Operation::MemSetAccess},
    FailureCase{"MemExport", Operation::MemExport},
    FailureCase{"EventCreate", Operation::EventCreate, 2, 2,
      "cuEventCreate producer failed code="},
    FailureCase{"IpcGetEventHandle", Operation::IpcGetEventHandle, 2, 2,
      "cuIpcGetEventHandle producer failed code="},
    FailureCase{"Socket", Operation::Socket},
    FailureCase{"Bind", Operation::Bind},
    FailureCase{"Listen", Operation::Listen, 2},
    FailureCase{"ExportGetFd", Operation::ExportGetFd, 1, 2,
      "exported FD F_GETFD failed:"},
    FailureCase{"ExportSetFd", Operation::ExportSetFd, 1, 2,
      "exported FD F_SETFD failed:"},
    FailureCase{"ListenerSetFd", Operation::ListenerSetFd, 1, 1,
      "listener F_SETFD failed:"},
    FailureCase{"ThreadCreate", Operation::ThreadCreate, 2, 2,
      "FD server thread creation failed:"},
    FailureCase{"EventContextPush", Operation::ContextPush, 2, 3,
      "Producer event context activation failed code="}),
  [](const ::testing::TestParamInfo<FailureCase> & info) {return info.param.name;});

struct ContextFailureCase
{
  const char * name;
  Operation operation;
  unsigned occurrence;
  unsigned metadata;
  unsigned releases;
  const char * diagnostic = nullptr;
};

class ProducerContextInitialisation :
  public InitialisationFixture, public ::testing::WithParamInterface<ContextFailureCase> {};

TEST_P(ProducerContextInitialisation, EarlyFailureReturnsEpochAndAllowsRetry)
{
  const auto & failure = GetParam();
  const auto operation = failure.operation;
  g3_cleanup::arm(operation, failure.occurrence, false, false);
  expect_failed_start(operation, failure.metadata, 0, failure.diagnostic);
  if (operation != Operation::ContextPush) {EXPECT_EQ(delta(operation), failure.occurrence);}
  EXPECT_EQ(delta(Operation::PrimaryRetain), 1u);
  EXPECT_EQ(delta(Operation::MemAddressReserve), 0u);
  EXPECT_EQ(delta(Operation::MemCreate), 0u);
  EXPECT_EQ(delta(Operation::EventCreate), 0u);
  EXPECT_EQ(delta(Operation::PrimaryRelease), failure.releases);
  EXPECT_EQ(delta(Operation::ContextPush) - g3_cleanup::injections(Operation::ContextPush),
    delta(Operation::ContextPop));
  if (operation == Operation::PrimaryRetain) {EXPECT_EQ(delta(Operation::ContextPush), 0u);}

  expect_restart(operation);
}

INSTANTIATE_TEST_SUITE_P(
  Context, ProducerContextInitialisation,
  ::testing::Values(
    ContextFailureCase{"PrimaryRetain", Operation::PrimaryRetain, 1, 0, 0,
      "Failed to retain CUDA primary context:"},
    ContextFailureCase{"AllocationContextPush", Operation::ContextPush, 1, 2, 1,
      "Failed to activate CUDA context:"},
    ContextFailureCase{"Granularity", Operation::Granularity, 1, 2, 1,
      "cuMemGetAllocationGranularity failed:"},
    ContextFailureCase{"MetadataTruncateFirst", Operation::MetadataTruncate, 1, 1, 1,
      "buffer_metadata:create ftruncate failed"},
    ContextFailureCase{"MetadataTruncateSecond", Operation::MetadataTruncate, 2, 2, 1,
      "buffer_metadata:create ftruncate failed"},
    ContextFailureCase{"MetadataMapFirst", Operation::MetadataMap, 1, 1, 1,
      "buffer_metadata:create mmap failed"},
    ContextFailureCase{"MetadataMapSecond", Operation::MetadataMap, 2, 2, 1,
      "buffer_metadata:create mmap failed"}),
  [](const ::testing::TestParamInfo<ContextFailureCase> & info) {return info.param.name;});

class ProducerAllocationException :
  public InitialisationFixture, public ::testing::WithParamInterface<FailureCase> {};

TEST_P(ProducerAllocationException, RollbackPrecedesRethrowAndAllowsRetry)
{
  const auto & failure = GetParam();
  g3_cleanup::arm(failure.operation, 2, false, false);
  expect_thrown_start(failure.operation, failure.sockets);
  EXPECT_EQ(delta(failure.operation), 2u);
  EXPECT_EQ(delta(Operation::PrimaryRetain), 1u);
  EXPECT_EQ(delta(Operation::MemCreate), failure.sockets);
  EXPECT_EQ(delta(Operation::EventCreate), 0u);
  expect_balanced_resources();
  expect_restart(failure.operation);
}

INSTANTIATE_TEST_SUITE_P(
  Exceptions, ProducerAllocationException,
  ::testing::Values(
    FailureCase{"MetadataAllocation", Operation::MetadataAllocation, 0},
    FailureCase{"ThreadAllocation", Operation::ThreadAllocation, 2}),
  [](const ::testing::TestParamInfo<FailureCase> & info) {return info.param.name;});

TEST_F(InitialisationFixture, CloexecFallbackKeepsDescriptorsPrivateAndAllowsRetry)
{
  g3_cleanup::arm(Operation::SocketFallback, 2, false, false);
  expect_fallback_start();
  expect_restart(Operation::SocketFallback);
}

TEST_F(InitialisationFixture, SocketCollisionPreservesExistingNameAndAllowsRetry)
{
  std::array<unsigned char, 16> uuid{};
  uuid_generate(uuid.data());
  char text[37]{};
  uuid_unparse_lower(uuid.data(), text);
  const std::string path = std::string("/tmp/cuda_memory_pool_") + text + ".sock";
  const int fd = open(path.c_str(), O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
  ASSERT_GE(fd, 0);
  struct OwnedCollision
  {
    const std::string & path;
    ~OwnedCollision() {unlink(path.c_str());}
  } owned{path};
  const auto written = write(fd, "sentinel", 8);
  struct stat original{};
  const int stated = fstat(fd, &original);
  close(fd);
  ASSERT_EQ(written, 8);
  ASSERT_EQ(stated, 0);
  const auto expect_preserved = [&]() {
      struct stat current{};
      ASSERT_EQ(lstat(path.c_str(), &current), 0);
      EXPECT_EQ(current.st_dev, original.st_dev);
      EXPECT_EQ(current.st_ino, original.st_ino);
      EXPECT_TRUE(S_ISREG(current.st_mode));
      char value[8]{};
      std::ifstream input(path);
      input.read(value, sizeof(value));
      ASSERT_EQ(input.gcount(), 8);
      EXPECT_EQ(std::string(value, sizeof(value)), "sentinel");
    };
  g3_cleanup::collision_uuid(uuid);
  g3_cleanup::arm(Operation::SocketCollision, 2, false, false);
  expect_failed_start(Operation::SocketCollision, 2, 1, "bind(");
  expect_preserved();
  expect_restart(Operation::SocketCollision);
  expect_preserved();
}
}  // namespace
