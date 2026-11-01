"""Isolated V1 synthetic banking domain."""
from .common import DomainError
from .service import Service

__all__ = ["Service", "DomainError"]
