"""Optional open-weight entailment check for unresolved claim evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from time import perf_counter
from typing import Protocol

import mlflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors.platform import DeadlineExceeded, TemporarilyUnavailable, TooManyRequests
from databricks.sdk.service.serving import (
    ChatMessage,
    ChatMessageRole,
    QueryEndpointResponse,
)

from trustdesk.ladder import (
    CheckFinding,
    ClaimEvidence,
    CostTier,
    EvidenceCoordinate,
    EvidenceItem,
    OutcomeKind,
)
from trustdesk.marks import Mark

PROMPT_VERSION = "entailment-v1"
PARSER_VERSION = "structured-items-v1"
DEFAULT_LLAMA_ENDPOINT = "databricks-meta-llama-3-1-8b-instruct"
_SYSTEM_PROMPT = """Judge whether each evidence item supports, conflicts with, or is irrelevant
to the claimed capability.
Use uncertain only when the item is related but cannot be decided. Return JSON only with this exact shape:
{"items":[{"field":"description","item_index":0,
"outcome":"support|conflict|irrelevant|uncertain","rationale":"short reason"}]}
Return exactly one item for every supplied field and item_index. Do not use outside knowledge."""


@dataclass(frozen=True)
class ModelRequest:
    """One claim-level request containing every unresolved evidence item."""

    request_id: str
    capability: str
    items: tuple[EvidenceItem, ...]
    prompt_version: str = PROMPT_VERSION
    attempt: int = 0


@dataclass(frozen=True)
class ModelReply:
    """Raw model content plus receipt-ready serving metadata."""

    content: str
    endpoint: str
    model: str
    latency_ms: float


class ModelClient(Protocol):
    """External boundary used by the check and in-memory test adapters."""

    def classify(self, request: ModelRequest) -> ModelReply: ...


class ServingEndpointClient(Protocol):
    """Subset of the Databricks serving API used by the production adapter."""

    def query(
        self,
        name: str,
        *,
        client_request_id: str | None = None,
        messages: list[ChatMessage] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        usage_context: dict[str, str] | None = None,
    ) -> QueryEndpointResponse: ...


class ModelTransientError(RuntimeError):
    """A serving failure that may recover after one retry."""


class ModelRateLimitError(ModelTransientError):
    """The serving endpoint rejected the request for quota or rate limit."""


class ModelTimeoutError(ModelTransientError):
    """The serving endpoint did not respond within the request deadline."""


def _messages(request: ModelRequest) -> list[ChatMessage]:
    payload = {
        "capability": request.capability,
        "items": [
            {
                "field": item.coordinate.field,
                "item_index": item.coordinate.item_index,
                "text": item.text,
            }
            for item in request.items
        ],
    }
    return [
        ChatMessage(role=ChatMessageRole.SYSTEM, content=_SYSTEM_PROMPT),
        ChatMessage(
            role=ChatMessageRole.USER,
            content=json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        ),
    ]


class DatabricksModelClient:
    """Workspace-authenticated adapter for one open-weight Foundation Model endpoint."""

    def __init__(
        self,
        endpoint: str = DEFAULT_LLAMA_ENDPOINT,
        serving: ServingEndpointClient | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._serving = serving

    def _client(self) -> ServingEndpointClient:
        if self._serving is None:
            self._serving = WorkspaceClient().serving_endpoints
        return self._serving

    def classify(self, request: ModelRequest) -> ModelReply:
        started = perf_counter()
        attributes: dict[str, object] = {
            "trustdesk.endpoint": self.endpoint,
            "trustdesk.prompt_version": request.prompt_version,
            "trustdesk.parser_version": PARSER_VERSION,
            "trustdesk.retry_count": request.attempt,
            "trustdesk.request_id": request.request_id,
            "trustdesk.item_count": len(request.items),
        }
        try:
            with mlflow.start_span(
                name="trustdesk.llama_entailment",
                span_type="LLM",
                attributes=attributes,
            ) as span:
                response = self._client().query(
                    self.endpoint,
                    client_request_id=request.request_id,
                    messages=_messages(request),
                    max_tokens=800,
                    temperature=0.0,
                    usage_context={"application": "trustdesk", "purpose": "batch_entailment"},
                )
                latency_ms = round((perf_counter() - started) * 1000, 3)
                choices = response.choices or []
                if len(choices) != 1 or choices[0].message is None or choices[0].message.content is None:
                    raise ValueError("invalid model output")
                model = response.model or response.served_model_name or self.endpoint
                span.set_attributes(
                    {
                        "trustdesk.model": model,
                        "trustdesk.latency_ms": latency_ms,
                    }
                )
                return ModelReply(
                    content=choices[0].message.content,
                    endpoint=self.endpoint,
                    model=model,
                    latency_ms=latency_ms,
                )
        except TooManyRequests:
            raise ModelRateLimitError from None
        except (DeadlineExceeded, TemporarilyUnavailable, TimeoutError):
            raise ModelTimeoutError from None


@dataclass(frozen=True)
class _ModelFinding:
    coordinate: EvidenceCoordinate
    outcome: str
    rationale: str


def _request(evidence: ClaimEvidence) -> ModelRequest:
    payload = {
        "capability": evidence.claim.capability,
        "items": [
            {
                "field": item.coordinate.field,
                "item_index": item.coordinate.item_index,
                "text": item.text,
            }
            for item in evidence.items
        ],
        "prompt_version": PROMPT_VERSION,
        "record_key": evidence.claim.record_key,
    }
    request_id = sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return ModelRequest(
        request_id=request_id,
        capability=evidence.claim.capability,
        items=evidence.items,
    )


def _parse(content: str, items: tuple[EvidenceItem, ...]) -> tuple[_ModelFinding, ...]:
    payload = json.loads(content)
    if not isinstance(payload, dict) or set(payload) != {"items"} or not isinstance(payload["items"], list):
        raise ValueError("invalid model output")
    findings: list[_ModelFinding] = []
    for raw in payload["items"]:
        if not isinstance(raw, dict) or set(raw) != {"field", "item_index", "outcome", "rationale"}:
            raise ValueError("invalid model output")
        field = raw["field"]
        item_index = raw["item_index"]
        outcome = raw["outcome"]
        rationale = raw["rationale"]
        if (
            not isinstance(field, str)
            or not isinstance(item_index, int)
            or outcome not in {"support", "conflict", "irrelevant", "uncertain"}
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            raise ValueError("invalid model output")
        findings.append(_ModelFinding(EvidenceCoordinate(field, item_index), outcome, rationale.strip()))

    expected = tuple(item.coordinate for item in items)
    observed = tuple(finding.coordinate for finding in findings)
    if len(observed) != len(set(observed)) or set(observed) != set(expected):
        raise ValueError("invalid model output")
    by_coordinate = {finding.coordinate: finding for finding in findings}
    return tuple(by_coordinate[coordinate] for coordinate in expected)


def _check_finding(finding: _ModelFinding, reply: ModelReply) -> CheckFinding:
    marks = {
        "support": Mark.SUPPORTS,
        "conflict": Mark.CONFLICTS,
        "irrelevant": Mark.SILENT,
    }
    mark = marks.get(finding.outcome)
    return CheckFinding(
        kind=OutcomeKind.DECISION if mark is not None else OutcomeKind.ABSTENTION,
        coordinate=finding.coordinate,
        mark=mark,
        rationale=(
            f"{finding.rationale} "
            f"[model={reply.model}; prompt={PROMPT_VERSION}; parser={PARSER_VERSION}]"
        ),
    )


def _processing_failures(evidence: ClaimEvidence, reason: str) -> tuple[CheckFinding, ...]:
    return tuple(
        CheckFinding(
            kind=OutcomeKind.PROCESSING_FAILURE,
            coordinate=item.coordinate,
            mark=None,
            rationale=reason,
        )
        for item in evidence.items
    )


class LlamaEntailmentCheck:
    """Classify one unresolved evidence bundle with one open-weight model call."""

    check_id = "llama_entailment"
    implementation_version = "1.0.0"
    cost_tier = CostTier.METERED

    def __init__(self, client: ModelClient | None = None) -> None:
        self.client = client or DatabricksModelClient()
        self._results: dict[str, tuple[CheckFinding, ...]] = {}

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        request = _request(evidence)
        cached = self._results.get(request.request_id)
        if cached is not None:
            return cached
        for attempt in range(2):
            try:
                attempted_request = replace(request, attempt=attempt)
                reply = self.client.classify(attempted_request)
                findings = _parse(reply.content, evidence.items)
                result = tuple(_check_finding(finding, reply) for finding in findings)
                self._results = {**self._results, request.request_id: result}
                return result
            except ModelTransientError as error:
                if attempt == 0:
                    continue
                category = "rate limit" if isinstance(error, ModelRateLimitError) else "timeout"
                result = _processing_failures(
                    evidence,
                    f"Model check failed after one retry ({category}).",
                )
                self._results = {**self._results, request.request_id: result}
                return result
            except (TypeError, ValueError):
                if attempt == 0:
                    continue
                result = _processing_failures(
                    evidence,
                    "Model check failed after one retry (invalid model output).",
                )
                self._results = {**self._results, request.request_id: result}
                return result
        raise AssertionError("unreachable")
