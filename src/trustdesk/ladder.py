"""Configured check execution for one claim and its parsed evidence bundle."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import Protocol, runtime_checkable

from trustdesk.marks import FIELDS, Mark
from trustdesk.models import Claim, FacilityRecord

DEFAULT_CHECKS_CONFIG = Path("config/checks.toml")
_CHECK_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class CostTier(StrEnum):
    """Execution cost used by the pipeline to order otherwise independent checks."""

    FREE = "free"
    METERED = "metered"


class OutcomeKind(StrEnum):
    """The three answers every check may return for an evidence item."""

    DECISION = "decision"
    ABSTENTION = "abstention"
    PROCESSING_FAILURE = "processing_failure"


@dataclass(frozen=True, order=True)
class EvidenceCoordinate:
    """Stable location of one item within a parsed facility record."""

    field: str
    item_index: int

    def __post_init__(self) -> None:
        if self.field not in FIELDS or self.item_index < 0:
            raise ValueError("invalid evidence coordinate")


@dataclass(frozen=True)
class EvidenceItem:
    """Exact parsed evidence text at one field and item index."""

    coordinate: EvidenceCoordinate
    text: str | None


@dataclass(frozen=True)
class ClaimEvidence:
    """One asserted claim plus the parsed evidence items a check may judge."""

    claim: Claim
    items: tuple[EvidenceItem, ...]
    source_urls: tuple[str, ...]

    def __post_init__(self) -> None:
        coordinates = tuple(item.coordinate for item in self.items)
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("duplicate evidence coordinate")

    @classmethod
    def from_record(cls, claim: Claim, record: FacilityRecord) -> ClaimEvidence:
        """Build a complete item bundle from a validated record, including empty fields."""
        if claim.record_key != record.record_key:
            raise ValueError("claim and evidence record keys differ")
        return cls(
            claim=claim,
            items=(
                EvidenceItem(EvidenceCoordinate("description", 0), record.description),
                *_array_items("capability", record.capability),
                *_array_items("equipment", record.equipment),
                *_array_items("procedure", record.procedure),
            ),
            source_urls=record.source_urls,
        )

    def unresolved(self, items: tuple[EvidenceItem, ...]) -> ClaimEvidence:
        """Keep claim and row provenance while narrowing a later check to unresolved items."""
        return ClaimEvidence(claim=self.claim, items=items, source_urls=self.source_urls)


def _array_items(field: str, values: tuple[str, ...]) -> tuple[EvidenceItem, ...]:
    if not values:
        return (EvidenceItem(EvidenceCoordinate(field, 0), None),)
    return tuple(EvidenceItem(EvidenceCoordinate(field, index), value) for index, value in enumerate(values))


@dataclass(frozen=True)
class CheckFinding:
    """A check's answer before the pipeline attaches implementation metadata."""

    kind: OutcomeKind
    coordinate: EvidenceCoordinate
    mark: Mark | None
    rationale: str
    span: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, OutcomeKind):
            raise ValueError("invalid outcome kind")
        if not isinstance(self.coordinate, EvidenceCoordinate):
            raise ValueError("invalid evidence coordinate")
        if self.mark is not None and not isinstance(self.mark, Mark):
            raise ValueError("invalid mark")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("check rationale is required")
        if (self.kind is OutcomeKind.DECISION) != (self.mark is not None):
            raise ValueError("only decisions may carry a mark")


@dataclass(frozen=True)
class CheckAttempt:
    """Receipt-ready record of one check's attempt on one evidence item."""

    kind: OutcomeKind
    coordinate: EvidenceCoordinate
    evidence_text: str | None
    mark: Mark | None
    check_id: str
    implementation_version: str
    cost_tier: CostTier
    rationale: str
    span: tuple[int, int] | None = None


@dataclass(frozen=True)
class CheckRun:
    """Decisions, still-unresolved evidence, and the complete ordered attempt history."""

    decisions: tuple[CheckAttempt, ...]
    unresolved: tuple[EvidenceItem, ...]
    attempt_history: tuple[CheckAttempt, ...]


@runtime_checkable
class Check(Protocol):
    """The only interface a new check implementation must satisfy."""

    check_id: str
    implementation_version: str
    cost_tier: CostTier

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]: ...


class CheckConfigurationError(ValueError):
    """Raised when configured check code or metadata cannot satisfy the interface."""


class _CheckContractError(ValueError):
    pass


def _validate_check(check: Check) -> None:
    if not _CHECK_ID.fullmatch(check.check_id):
        raise CheckConfigurationError("invalid check configuration")
    if not _VERSION.fullmatch(check.implementation_version):
        raise CheckConfigurationError("invalid check configuration")
    if not isinstance(check.cost_tier, CostTier):
        raise CheckConfigurationError("invalid check configuration")


def _ordered_checks(checks: Sequence[Check]) -> tuple[Check, ...]:
    checks = tuple(checks)
    for check in checks:
        _validate_check(check)
    if len({check.check_id for check in checks}) != len(checks):
        raise CheckConfigurationError("invalid check configuration")
    priority = {CostTier.FREE: 0, CostTier.METERED: 1}
    return tuple(sorted(checks, key=lambda check: priority[check.cost_tier]))


def load_checks(path: Path = DEFAULT_CHECKS_CONFIG) -> tuple[Check, ...]:
    """Load check implementations from TOML and order them by cost, then config position."""
    try:
        config = tomllib.loads(path.read_text())
        entries = config["checks"]
        if not isinstance(entries, list) or not entries or not all(isinstance(entry, str) for entry in entries):
            raise CheckConfigurationError("invalid check configuration")

        checks: list[Check] = []
        for entry in entries:
            module_name, separator, class_name = entry.partition(":")
            if not separator or not module_name or not class_name:
                raise CheckConfigurationError("invalid check configuration")
            check_object = getattr(import_module(module_name), class_name)()
            if not isinstance(check_object, Check):
                raise CheckConfigurationError("invalid check configuration")
            checks.append(check_object)
        return _ordered_checks(checks)
    except CheckConfigurationError:
        raise
    except (ImportError, AttributeError, KeyError, OSError, TypeError, tomllib.TOMLDecodeError):
        raise CheckConfigurationError("invalid check configuration") from None


def _validated_findings(
    findings: object,
    items: tuple[EvidenceItem, ...],
) -> tuple[CheckFinding | None, ...]:
    if not isinstance(findings, tuple) or not all(isinstance(finding, CheckFinding) for finding in findings):
        raise _CheckContractError("invalid check output")
    by_coordinate = {finding.coordinate: finding for finding in findings}
    coordinates = tuple(item.coordinate for item in items)
    if len(by_coordinate) != len(findings) or not set(by_coordinate).issubset(coordinates):
        raise _CheckContractError("invalid check output")
    return tuple(by_coordinate.get(coordinate) for coordinate in coordinates)


def _failure_attempt(check: Check, item: EvidenceItem, error: Exception) -> CheckAttempt:
    return CheckAttempt(
        kind=OutcomeKind.PROCESSING_FAILURE,
        coordinate=item.coordinate,
        evidence_text=item.text,
        mark=None,
        check_id=check.check_id,
        implementation_version=check.implementation_version,
        cost_tier=check.cost_tier,
        rationale=f"Check could not process this item ({type(error).__name__}).",
    )


def _attempt(check: Check, item: EvidenceItem, finding: CheckFinding) -> CheckAttempt:
    return CheckAttempt(
        kind=finding.kind,
        coordinate=item.coordinate,
        evidence_text=item.text,
        mark=finding.mark,
        check_id=check.check_id,
        implementation_version=check.implementation_version,
        cost_tier=check.cost_tier,
        rationale=finding.rationale,
        span=finding.span,
    )


def _omitted_attempt(check: Check, item: EvidenceItem) -> CheckAttempt:
    return CheckAttempt(
        kind=OutcomeKind.ABSTENTION,
        coordinate=item.coordinate,
        evidence_text=item.text,
        mark=None,
        check_id=check.check_id,
        implementation_version=check.implementation_version,
        cost_tier=check.cost_tier,
        rationale="Check returned no finding for this item.",
    )


def run_checks(evidence: ClaimEvidence, checks: Sequence[Check]) -> CheckRun:
    """Run each check once on unresolved evidence; the first decision per coordinate wins."""
    original_items = evidence.items
    unresolved = {item.coordinate: item for item in original_items}
    decisions: dict[EvidenceCoordinate, CheckAttempt] = {}
    history: list[CheckAttempt] = []

    for check in _ordered_checks(checks):
        if not unresolved:
            break
        current_items = tuple(unresolved.values())
        try:
            findings = _validated_findings(check.evaluate(evidence.unresolved(current_items)), current_items)
        except Exception as error:
            history.extend(_failure_attempt(check, item, error) for item in current_items)
            continue

        for item, finding in zip(current_items, findings, strict=True):
            attempt = _omitted_attempt(check, item) if finding is None else _attempt(check, item, finding)
            history.append(attempt)
            if attempt.kind is OutcomeKind.DECISION:
                decisions[item.coordinate] = attempt
                del unresolved[item.coordinate]

    return CheckRun(
        decisions=tuple(decisions[item.coordinate] for item in original_items if item.coordinate in decisions),
        unresolved=tuple(unresolved.values()),
        attempt_history=tuple(history),
    )
