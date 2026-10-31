#pragma once

#include <cuda.h>

namespace subscriber_failure
{
enum class Operation {Record, Query, Destroy, Wait, Create, ContextPush, ThreadCreate, QueueAllocation, WorkerAllocation, WorkerRequeueAllocation, Count};
enum class PopOrigin {Releaser, Worker};

// Each CTest filter runs in its own process because poison is process-lifetime.
void arm(Operation operation, unsigned occurrence = 1);
unsigned calls(Operation operation);
unsigned failures(Operation operation);
void disarm();
// True only after the observed completion worker enters its subsequent idle wait.
bool worker_idle();
// Counts the worker-only synthetic NOT_READY queries before failed re-enqueue.
unsigned worker_not_ready_queries();

void gate_pop(PopOrigin origin, bool fail);
bool wait_for_pop();
void release_pop();
unsigned pop_calls();
unsigned pop_failures();
}  // namespace subscriber_failure
