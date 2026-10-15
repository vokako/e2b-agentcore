"""Map AWS errors onto the official E2B exception classes.

Callers keep writing `except e2b.NotFoundException:` etc. and it works the same against
this provider, because we raise the e2b classes themselves rather than look-alikes.
"""
from __future__ import annotations

import functools

from botocore.exceptions import ClientError, ReadTimeoutError
from e2b import (AuthenticationException, NotFoundException, RateLimitException, SandboxException,
                 TimeoutException)


class NotSupportedException(SandboxException, NotImplementedError):
    """An E2B feature AgentCore has no equivalent for. The message says why and what to use."""


def not_supported(feature: str, reason: str):
    def stub(*_a, **_k):
        raise NotSupportedException(f"{feature} is not supported on AgentCore: {reason}")
    stub.__agentcore_unsupported__ = reason  # read by tools/api_coverage.py
    stub.__name__ = feature.rsplit(".", 1)[-1]
    return stub


def map_aws_error(exc: Exception) -> Exception:
    if isinstance(exc, ReadTimeoutError):
        return TimeoutException(str(exc))
    if not isinstance(exc, ClientError):
        return exc
    code = exc.response.get("Error", {}).get("Code", "")
    msg = exc.response.get("Error", {}).get("Message", str(exc))
    if code in ("ResourceNotFoundException", "NotFoundException"):
        return NotFoundException(msg)
    if code in ("ThrottlingException", "ServiceQuotaExceededException", "TooManyRequestsException"):
        return RateLimitException(msg)
    if code in ("AccessDeniedException", "UnrecognizedClientException", "ExpiredTokenException"):
        return AuthenticationException(msg)
    return SandboxException(f"{code}: {msg}")


def aws_errors(fn):
    """Decorator: re-raise boto errors as E2B exceptions, keeping the original as __cause__."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except (ClientError, ReadTimeoutError) as exc:
            raise map_aws_error(exc) from exc
    return wrapper
