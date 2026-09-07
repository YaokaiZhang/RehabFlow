"""Compatibility export for the live longitudinal result value object.

Provider-free longitudinal execution is retired. Accepted evaluation cases use
the service-backed runner in evals.combined.service_runner.
"""

from evals.longitudinal.result import LongitudinalResult

__all__ = ("LongitudinalResult",)
