#include "subscriber_failure.hpp"

#include <dlfcn.h>
#include <pthread.h>

#include <cerrno>
#include <cstddef>
#include <new>

#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <mutex>

namespace subscriber_failure
{
namespace
{
struct Failure
{
  std::atomic<bool> armed{false};
  std::atomic<unsigned> remaining{0};
  std::atomic<unsigned> calls{0};
  std::atomic<unsigned> failures{0};
};
std::array<Failure, static_cast<unsigned>(Operation::Count)> operations;
std::atomic<bool> pop_enabled{false};
std::atomic<PopOrigin> pop_origin{PopOrigin::Releaser};
std::atomic<bool> fail_pop{false};
std::atomic<unsigned> observed_pops{0};
std::atomic<unsigned> failed_pops{0};
std::mutex pop_mutex;
std::condition_variable pop_condition;
bool pop_entered = false;
bool pop_released = false;
thread_local unsigned push_depth = 0;
thread_local bool target_thread = false;
thread_local bool recorded_for_queue = false;
thread_local bool queue_window = false;
thread_local bool watch_worker_allocations = false;
thread_local bool worker_requeue_window = false;
std::atomic<bool> worker_wait_entered{false};
std::atomic<unsigned> not_ready_queries{0};

bool inject(Operation operation)
{
  auto & state = operations[static_cast<unsigned>(operation)];
  if (!state.armed.load()) {return false;}
  state.calls.fetch_add(1);
  if (state.remaining.load() == 0 || state.remaining.fetch_sub(1) != 1) {return false;}
  state.failures.fetch_add(1);
  return true;
}

void mark_thread(PopOrigin origin)
{
  if (pop_enabled.load() && pop_origin.load() == origin) {target_thread = true;}
}
}  // namespace

void arm(Operation operation, unsigned occurrence)
{
  auto & state = operations[static_cast<unsigned>(operation)];
  state.remaining.store(occurrence);
  state.armed.store(true);
}

unsigned calls(Operation operation)
{
  return operations[static_cast<unsigned>(operation)].calls.load();
}

unsigned failures(Operation operation)
{
  return operations[static_cast<unsigned>(operation)].failures.load();
}

void disarm()
{
  for (auto & state : operations) {state.armed.store(false);}
  pop_enabled.store(false);
  recorded_for_queue = false;
  queue_window = false;
}

void gate_pop(PopOrigin origin, bool fail)
{
  pop_origin.store(origin);
  fail_pop.store(fail);
  pop_enabled.store(true);
}

bool wait_for_pop()
{
  std::unique_lock<std::mutex> lock(pop_mutex);
  return pop_condition.wait_for(lock, std::chrono::seconds(3), [] {return pop_entered;});
}

void release_pop()
{
  {
    std::lock_guard<std::mutex> lock(pop_mutex);
    pop_released = true;
  }
  pop_condition.notify_all();
}

bool worker_idle() {return worker_wait_entered.load();}
unsigned worker_not_ready_queries() {return not_ready_queries.load();}

unsigned pop_calls() {return observed_pops.load();}
unsigned pop_failures() {return failed_pops.load();}
}  // namespace subscriber_failure

extern "C" CUresult CUDAAPI cuEventCreate(CUevent * event, unsigned flags)
{
  using namespace subscriber_failure;
  if (inject(Operation::Create)) {return CUDA_ERROR_OUT_OF_MEMORY;}
  using Function = CUresult (CUDAAPI *)(CUevent *, unsigned);
  static auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT, "cuEventCreate"));
  return original ? original(event, flags) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuEventRecord(CUevent event, CUstream stream)
{
  using namespace subscriber_failure;
  if (inject(Operation::Record)) {return CUDA_ERROR_UNKNOWN;}
  using Function = CUresult (CUDAAPI *)(CUevent, CUstream);
  static auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT, "cuEventRecord"));
  const auto result = original ? original(event, stream) : CUDA_ERROR_UNKNOWN;
  if (result == CUDA_SUCCESS) {
    mark_thread(PopOrigin::Releaser);
    recorded_for_queue = operations[static_cast<unsigned>(Operation::ThreadCreate)].armed.load() ||
      operations[static_cast<unsigned>(Operation::QueueAllocation)].armed.load();
  }
  return result;
}

extern "C" CUresult CUDAAPI cuEventQuery(CUevent event)
{
  using namespace subscriber_failure;
  if (inject(Operation::Query)) {return CUDA_ERROR_UNKNOWN;}
  if (operations[static_cast<unsigned>(Operation::WorkerRequeueAllocation)].armed.load()) {
    // Keep the same charged item pending until deque growth attempts an
    // allocation. Only this query's worker thread enters the injection window.
    not_ready_queries.fetch_add(1);
    worker_requeue_window = true;
    watch_worker_allocations = true;
    return CUDA_ERROR_NOT_READY;
  }
  using Function = CUresult (CUDAAPI *)(CUevent);
  static auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT, "cuEventQuery"));
  const auto result = original ? original(event) : CUDA_ERROR_UNKNOWN;
  if (result == CUDA_SUCCESS) {
    mark_thread(PopOrigin::Worker);
    if (operations[static_cast<unsigned>(Operation::WorkerAllocation)].armed.load()) {
      watch_worker_allocations = true;
    }
  }
  return result;
}

extern "C" CUresult CUDAAPI cuEventDestroy(CUevent event)
{
  using namespace subscriber_failure;
  if (inject(Operation::Destroy)) {return CUDA_ERROR_UNKNOWN;}
  using Function = CUresult (CUDAAPI *)(CUevent);
  static auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT, "cuEventDestroy_v2"));
  return original ? original(event) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuStreamWaitEvent(CUstream stream, CUevent event, unsigned flags)
{
  using namespace subscriber_failure;
  if (inject(Operation::Wait)) {return CUDA_ERROR_UNKNOWN;}
  using Function = CUresult (CUDAAPI *)(CUstream, CUevent, unsigned);
  static auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT, "cuStreamWaitEvent"));
  return original ? original(stream, event, flags) : CUDA_ERROR_UNKNOWN;
}

extern "C" CUresult CUDAAPI cuCtxPushCurrent(CUcontext context)
{
  if (subscriber_failure::inject(subscriber_failure::Operation::ContextPush)) {
    return CUDA_ERROR_INVALID_CONTEXT;
  }
  using Function = CUresult (CUDAAPI *)(CUcontext);
  static auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT, "cuCtxPushCurrent_v2"));
  const auto result = original ? original(context) : CUDA_ERROR_UNKNOWN;
  if (result == CUDA_SUCCESS) {++subscriber_failure::push_depth;}
  return result;
}

extern "C" CUresult CUDAAPI cuCtxPopCurrent(CUcontext * context)
{
  using namespace subscriber_failure;
  // Gate the outer guard, after nested event cleanup guards have restored.
  // This observes the worker's old ref-before-pop ordering as well as the fix.
  if (pop_enabled.load() && target_thread && push_depth == 1) {
    observed_pops.fetch_add(1);
    {
      std::unique_lock<std::mutex> lock(pop_mutex);
      pop_entered = true;
      pop_condition.notify_all();
      pop_condition.wait_for(lock, std::chrono::seconds(10), [] {return pop_released;});
    }
    if (fail_pop.exchange(false)) {
      failed_pops.fetch_add(1);
      return CUDA_ERROR_UNKNOWN;
    }
  }
  using Function = CUresult (CUDAAPI *)(CUcontext *);
  static auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT, "cuCtxPopCurrent_v2"));
  const auto result = original ? original(context) : CUDA_ERROR_UNKNOWN;
  if (result == CUDA_SUCCESS && push_depth != 0) {
    --push_depth;
    // Open the test-thread-only window after the CUDA completion record and
    // its outer context restoration, immediately before deferred submission.
    if (push_depth == 0 && recorded_for_queue) {
      recorded_for_queue = false;
      queue_window = true;
    }
  }
  return result;
}


extern "C" int pthread_create(
  pthread_t * thread, const pthread_attr_t * attributes,
  void * (*start)(void *), void * argument) noexcept
{
  using namespace subscriber_failure;
  if (queue_window && inject(Operation::ThreadCreate)) {
    queue_window = false;
    return EAGAIN;
  }
  using Function = int (*)(pthread_t *, const pthread_attr_t *, void * (*)(void *), void *);
  static auto real = reinterpret_cast<Function>(dlsym(RTLD_NEXT, "pthread_create"));
  return real ? real(thread, attributes, start, argument) : EAGAIN;
}

// Linker wrapping affects direct C++ allocations in this executable/static core,
// not CUDA driver DSO allocations. TLS windows separate the releasing test
// thread from the worker after its observed completion query.
extern "C" void * __real__Znwm(std::size_t size);
extern "C" void * __wrap__Znwm(std::size_t size)
{
  using namespace subscriber_failure;
  if (worker_requeue_window && inject(Operation::WorkerRequeueAllocation)) {
    worker_requeue_window = false;
    throw std::bad_alloc();
  }
  if (watch_worker_allocations && inject(Operation::WorkerAllocation)) {
    throw std::bad_alloc();
  }
  if (queue_window && inject(Operation::QueueAllocation)) {
    queue_window = false;
    throw std::bad_alloc();
  }
  return __real__Znwm(size);
}


extern "C" int pthread_cond_wait(pthread_cond_t * condition, pthread_mutex_t * mutex)
{
  using namespace subscriber_failure;
  // A success or synthetic NOT_READY query identified the worker. Its next
  // idle wait occurs after the release or failed requeue and its diagnostics.
  if (watch_worker_allocations) {worker_wait_entered.store(true);}
  using Function = int (*)(pthread_cond_t *, pthread_mutex_t *);
  static auto real = reinterpret_cast<Function>(dlsym(RTLD_NEXT, "pthread_cond_wait"));
  return real ? real(condition, mutex) : EINVAL;
}
