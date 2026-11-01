"""Host identities are capabilities, never model-provided JSON fields."""
from dataclasses import dataclass

LEGACY = 'legacy_source_v1'
MINIMAL = 'minimal_source_v2'


@dataclass(frozen=True)
class RunHandle:
    issuer: object
    instance_id: str
    epoch_id: str
    run_id: str
    job_id: str | None
    source_contract: str
    response_mode: str
    read_only: bool


@dataclass(frozen=True)
class AttemptHandle:
    run: RunHandle
    attempt_id: str


@dataclass(frozen=True)
class RecoveryHandle:
    issuer: object
    run_id: str
    attempt_id: str


def version(context):
    value = context.get('source_contract', LEGACY)
    if value not in {LEGACY, MINIMAL}:
        raise ValueError('SOURCE_PROTOCOL_MISMATCH')
    return value
