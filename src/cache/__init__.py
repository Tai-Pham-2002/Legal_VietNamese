"""High-level cache helpers + rate limiter."""

from .rate_limit import allow_request, RateLimitExceeded

__all__ = ["allow_request", "RateLimitExceeded"]
