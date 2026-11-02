"""Explicit CUDA Tensor ownership for generated Hermos endpoints.

Importing this package does not initialize CUDA or import Torch. Initialize one
GpuRuntime before constructing GPU endpoints, then register it before dispatch.
"""

from .runtime import GpuRuntime

__all__ = ['GpuRuntime']
