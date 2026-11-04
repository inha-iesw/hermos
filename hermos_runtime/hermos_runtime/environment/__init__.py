"""Resolve a CPU installation without importing rclpy, native extensions or CUDA."""

from .manifest import Installation, InstallationError, resolve_installation
from .peer import peer_environment

__all__ = ['Installation', 'InstallationError', 'resolve_installation', 'peer_environment']
