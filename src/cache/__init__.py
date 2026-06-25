"""High-level cache helpers + rate limiter."""

from .rate_limit import RateLimitExceeded, allow_request

__all__ = ["allow_request", "RateLimitExceeded"]
