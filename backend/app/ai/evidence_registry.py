"""Production registry for authorized evidence sources."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.ai.context_integrity import ContextIntegrity
from app.ai.evidence_types import EvidenceRequest, EvidenceSource


class EvidenceAuthorizationError(PermissionError):
    """Raised when a request exceeds deterministic source authorization."""


PRODUCTION_EVIDENCE_SOURCES = frozenset(EvidenceSource)
_REJECTED_PRODUCTION_NAMES = frozenset({
    "default_web_search",
    "web_search",
    "simulated_web_search",
    "fake_web_search",
})


class EvidenceRegistry:
    """Maps only authorized source types to injected adapters and plans requests."""

    def __init__(
        self,
        adapters: Mapping[EvidenceSource | str, Any] | None = None,
        *,
        production: bool = True,
    ) -> None:
        self._production = production
        self._adapters: dict[EvidenceSource, Any] = {}
        for source, adapter in (adapters or {}).items():
            self.register(source, adapter)

    @property
    def sources(self) -> frozenset[EvidenceSource]:
        if self._production:
            return PRODUCTION_EVIDENCE_SOURCES
        return frozenset(self._adapters)

    @property
    def registered_sources(self) -> frozenset[EvidenceSource]:
        return self.sources

    def register(self, source: EvidenceSource | str, adapter: Any) -> None:
        raw_source = str(source)
        if raw_source in _REJECTED_PRODUCTION_NAMES:
            raise ValueError(f"Evidence source '{raw_source}' is not admissible in production")
        try:
            source_value = source if isinstance(source, EvidenceSource) else EvidenceSource(raw_source)
        except ValueError as exc:
            raise ValueError(f"Unknown evidence source '{raw_source}'") from exc
        if self._production and source_value not in PRODUCTION_EVIDENCE_SOURCES:
            raise ValueError(f"Evidence source '{source_value}' is not admissible in production")
        adapter_source = getattr(adapter, "source", None)
        if adapter_source is not None and str(adapter_source) != source_value.value:
            raise ValueError(f"Adapter source mismatch for '{source_value.value}'")
        self._adapters[source_value] = adapter

    def adapter_for(self, source: EvidenceSource | str) -> Any:
        source_value = self._coerce_source(source)
        if source_value not in self.sources:
            raise EvidenceAuthorizationError(f"Evidence source '{source_value.value}' is not registered")
        if source_value not in self._adapters:
            raise LookupError(f"No adapter injected for '{source_value.value}'")
        return self._adapters[source_value]

    def plan(
        self,
        requests: Iterable[EvidenceRequest],
        integrity: ContextIntegrity,
    ) -> list[EvidenceRequest]:
        planned: list[EvidenceRequest] = []
        for request in requests:
            normalized = request if isinstance(request, EvidenceRequest) else EvidenceRequest.model_validate(request)
            source = normalized.source
            if source not in self.sources:
                raise EvidenceAuthorizationError(
                    f"Evidence source '{source.value}' is not registered"
                )
            allowance = integrity.allowance_for(source)
            if allowance is None or not allowance.visible:
                raise EvidenceAuthorizationError(
                    f"Evidence source '{source.value}' is not allowed by Context Integrity"
                )
            allowed_ids = set(allowance.source_ids)
            forbidden_ids = [source_id for source_id in normalized.source_ids if source_id not in allowed_ids]
            if forbidden_ids:
                raise EvidenceAuthorizationError(
                    f"Source identifier(s) not allowed for '{source.value}': {', '.join(forbidden_ids)}"
                )
            planned.append(normalized)
        return planned

    def plan_requests(
        self,
        requests: Iterable[EvidenceRequest],
        integrity: ContextIntegrity,
    ) -> list[EvidenceRequest]:
        return self.plan(requests, integrity)

    def _coerce_source(self, source: EvidenceSource | str) -> EvidenceSource:
        try:
            return source if isinstance(source, EvidenceSource) else EvidenceSource(str(source))
        except ValueError as exc:
            raise EvidenceAuthorizationError(f"Unknown evidence source '{source}'") from exc


class ProductionEvidenceRegistry(EvidenceRegistry):
    """Production registry with no external/simulated web-search source."""

    def __init__(self, adapters: Mapping[EvidenceSource | str, Any] | None = None) -> None:
        super().__init__(adapters, production=True)


def build_production_evidence_registry(
    adapters: Mapping[EvidenceSource | str, Any] | None = None,
    *,
    session_factory: Any | None = None,
    retrieval: Any | None = None,
) -> ProductionEvidenceRegistry:
    if adapters is None and session_factory is not None and retrieval is not None:
        from app.ai.evidence_adapters import build_production_adapters

        adapters = build_production_adapters(
            session_factory=session_factory,
            retrieval=retrieval,
        )
    return ProductionEvidenceRegistry(adapters)


production_evidence_registry = build_production_evidence_registry


AuthorizedEvidenceRegistry = ProductionEvidenceRegistry
