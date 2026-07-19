"""Behavior tests for the optional metered entailment check."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from databricks.sdk.service.serving import (
    ChatMessage,
    QueryEndpointResponse,
    V1ResponseChoiceElement,
)

from trustdesk.ladder import ClaimEvidence, CostTier, OutcomeKind, load_checks
from trustdesk.llm_check import (
    DatabricksModelClient,
    LlamaEntailmentCheck,
    ModelRateLimitError,
    ModelReply,
    ModelRequest,
    ModelTimeoutError,
)
from trustdesk.marks import Mark
from trustdesk.models import Claim, FacilityRecord


def claim_bundle() -> ClaimEvidence:
    record = FacilityRecord(
        record_key="record-1",
        facility_id="facility-1",
        name="Example Hospital",
        description="An ICU is available.",
        capability=("No ICU is maintained on site.",),
        equipment=("X-ray machine",),
        procedure=("Critical care transfer policy",),
        source_urls=(),
        region="Bihar",
    )
    return ClaimEvidence.from_record(Claim(record.record_key, "ICU"), record)


@dataclass
class ScriptedModelClient:
    replies: list[str | Exception]
    requests: list[ModelRequest] = field(default_factory=list)

    def classify(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ModelReply(
            content=reply,
            endpoint="test-llama-endpoint",
            model="test-llama",
            latency_ms=12.5,
        )


@dataclass
class FakeServingEndpoints:
    content: str
    calls: list[dict[str, object]] = field(default_factory=list)

    def query(
        self,
        name: str,
        *,
        client_request_id: str | None = None,
        messages: list[ChatMessage] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        usage_context: dict[str, str] | None = None,
    ) -> QueryEndpointResponse:
        self.calls.append(
            {
                "name": name,
                "client_request_id": client_request_id,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "usage_context": usage_context,
            }
        )
        return QueryEndpointResponse(
            choices=[
                V1ResponseChoiceElement(
                    index=0,
                    message=ChatMessage(content=self.content),
                )
            ],
            model="test-served-llama",
        )


def structured_reply(*outcomes: str) -> str:
    evidence = claim_bundle()
    return json.dumps(
        {
            "items": [
                {
                    "field": item.coordinate.field,
                    "item_index": item.coordinate.item_index,
                    "outcome": outcome,
                    "rationale": f"Model classified this item as {outcome}.",
                }
                for item, outcome in zip(evidence.items, outcomes, strict=True)
            ]
        }
    )


def test_one_bundle_call_maps_structured_outcomes_to_the_existing_check_contract():
    client = ScriptedModelClient(
        [structured_reply("support", "conflict", "irrelevant", "uncertain")]
    )
    check = LlamaEntailmentCheck(client=client)

    findings = check.evaluate(claim_bundle())

    assert len(client.requests) == 1
    assert len(client.requests[0].items) == 4
    assert [finding.kind for finding in findings] == [
        OutcomeKind.DECISION,
        OutcomeKind.DECISION,
        OutcomeKind.DECISION,
        OutcomeKind.ABSTENTION,
    ]
    assert [finding.mark for finding in findings] == [
        Mark.SUPPORTS,
        Mark.CONFLICTS,
        Mark.SILENT,
        None,
    ]
    assert [finding.coordinate for finding in findings] == [
        item.coordinate for item in claim_bundle().items
    ]


def test_one_transient_rate_limit_retry_can_recover():
    client = ScriptedModelClient(
        [
            ModelRateLimitError(),
            structured_reply("support", "conflict", "irrelevant", "uncertain"),
        ]
    )
    check = LlamaEntailmentCheck(client=client)

    findings = check.evaluate(claim_bundle())

    assert len(client.requests) == 2
    assert [request.attempt for request in client.requests] == [0, 1]
    assert findings[0].kind is OutcomeKind.DECISION
    assert findings[0].mark is Mark.SUPPORTS


def test_malformed_output_after_one_retry_is_visible_processing_failure():
    client = ScriptedModelClient(["not json", '{"items": []}'])
    check = LlamaEntailmentCheck(client=client)

    findings = check.evaluate(claim_bundle())

    assert len(client.requests) == 2
    assert all(finding.kind is OutcomeKind.PROCESSING_FAILURE for finding in findings)
    assert all(finding.mark is None for finding in findings)
    assert all("invalid model output" in finding.rationale.lower() for finding in findings)


def test_duplicate_invocation_reuses_the_deterministic_result():
    client = ScriptedModelClient(
        [structured_reply("support", "conflict", "irrelevant", "uncertain")]
    )
    check = LlamaEntailmentCheck(client=client)

    first = check.evaluate(claim_bundle())
    repeated = check.evaluate(claim_bundle())

    assert repeated == first
    assert len(client.requests) == 1


@pytest.mark.parametrize("error_type", [ModelRateLimitError, ModelTimeoutError])
def test_exhausted_transient_retry_is_visible_and_redacted(
    error_type: type[ModelRateLimitError] | type[ModelTimeoutError],
):
    client = ScriptedModelClient(
        [error_type("secret endpoint detail"), error_type("secret endpoint detail")]
    )
    check = LlamaEntailmentCheck(client=client)

    findings = check.evaluate(claim_bundle())

    assert len(client.requests) == 2
    assert all(finding.kind is OutcomeKind.PROCESSING_FAILURE for finding in findings)
    assert all("secret endpoint detail" not in finding.rationale for finding in findings)


def test_databricks_and_in_memory_adapters_share_the_model_client_contract():
    content = structured_reply("support", "conflict", "irrelevant", "uncertain")
    request = ModelRequest(
        request_id="request-1",
        capability="ICU",
        items=claim_bundle().items,
    )
    in_memory = ScriptedModelClient([content])
    serving = FakeServingEndpoints(content)
    databricks = DatabricksModelClient(
        endpoint="databricks-meta-llama-3-1-8b-instruct",
        serving=serving,
    )

    for client in (in_memory, databricks):
        reply = client.classify(request)
        assert reply.content == content
        assert reply.endpoint
        assert reply.model
        assert reply.latency_ms >= 0

    assert serving.calls[0]["name"] == "databricks-meta-llama-3-1-8b-instruct"
    assert serving.calls[0]["client_request_id"] == "request-1"


def test_default_config_loads_only_free_checks():
    checks = load_checks(Path("config/checks.toml"))

    assert [check.check_id for check in checks] == ["presence", "vocabulary"]
    assert all(check.cost_tier is CostTier.FREE for check in checks)


def test_llama_check_can_be_enabled_with_one_config_entry(tmp_path: Path):
    config = tmp_path / "checks.toml"
    config.write_text(
        'checks = ["trustdesk.check_presence:PresenceCheck", '
        '"trustdesk.llm_check:LlamaEntailmentCheck"]\n'
    )

    checks = load_checks(config)

    assert [check.check_id for check in checks] == ["presence", "llama_entailment"]
    assert checks[-1].cost_tier is CostTier.METERED
