"""Bootstrap CPU peers before importing their application modules."""

from .entry import run, run_executable

__all__ = ['run', 'run_executable']
