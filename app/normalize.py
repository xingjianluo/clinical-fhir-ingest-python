"""Adapters that normalize supported external representations to the internal model.

Supported external versions:
  - ``fhir-r4``: Patient + Observation shape, ``meta.versionId`` ignored here.
  - ``fhir-r5``: same shape but identifiers arrive under ``identifier`` entries
    with ``type.coding`` and observations may use ``valueString``.

Any rejection raises IngestionError with a distinguishable ErrorCode and the
original offending fragment preserved in ``detail``.
"""
from __future__ import annotations

from typing import Any

from app.errors import ErrorCode, IngestionError
from app.models import (
    KNOWN_CODING_SYSTEMS,
    Coding,
    ObservationRecord,
    PatientResource,
)

#: External representation versions this service accepts.
SUPPORTED_VERSIONS: frozenset[str] = frozenset({"fhir-r4", "fhir-r5"})

_REQUIRED_IDENTIFIER_KEYS = ("mrn",)


def _fail(code: ErrorCode, message: str, detail: Any) -> None:
    raise IngestionError(code, message, detail=detail)


def _require(payload: dict, key: str, context: str) -> Any:
    value = payload.get(key)
    if value is None or value == "":
        _fail(
            ErrorCode.MISSING_REQUIRED_FIELD,
            f"required field '{key}' missing in {context}",
            {"field": key, "context": context, "raw": payload},
        )
    return value


def _normalize_coding(raw: Any, context: str) -> Coding:
    if not isinstance(raw, dict):
        _fail(ErrorCode.MALFORMED_PAYLOAD, f"coding must be an object in {context}", raw)
    system = _require(raw, "system", context)
    code = _require(raw, "code", context)
    if system not in KNOWN_CODING_SYSTEMS:
        _fail(
            ErrorCode.UNKNOWN_CODING_SYSTEM,
            f"coding system '{system}' is not recognised",
            {"system": system, "code": code, "context": context},
        )
    return Coding(system=system, code=code, display=raw.get("display"))


def _normalize_identifiers(payload: dict, source_version: str) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    if source_version == "fhir-r4":
        # R4 profile used here: identifiers as a flat map plus demographics.
        raw_map = payload.get("identifiers", {})
        if not isinstance(raw_map, dict):
            _fail(ErrorCode.MALFORMED_PAYLOAD, "'identifiers' must be an object", raw_map)
        identifiers.update({str(k): str(v) for k, v in raw_map.items()})
    else:  # fhir-r5
        # R5 profile: list of identifier entries with a 'use'/'type' label.
        raw_list = payload.get("identifier", [])
        if not isinstance(raw_list, list):
            _fail(ErrorCode.MALFORMED_PAYLOAD, "'identifier' must be a list", raw_list)
        for entry in raw_list:
            if not isinstance(entry, dict) or "type" not in entry or "value" not in entry:
                _fail(ErrorCode.MALFORMED_PAYLOAD,
                      "identifier entries need 'type' and 'value'", entry)
            identifiers[str(entry["type"])] = str(entry["value"])
    # Demographics are direct identifiers too.
    for key in ("name", "birth_date", "address", "phone", "email"):
        if payload.get(key):
            identifiers[key] = str(payload[key])
    for required in _REQUIRED_IDENTIFIER_KEYS:
        if required not in identifiers:
            _fail(
                ErrorCode.MISSING_REQUIRED_FIELD,
                f"required identifier '{required}' missing",
                {"field": required, "raw": payload},
            )
    return identifiers


def _normalize_observations(payload: dict, source_version: str) -> list[ObservationRecord]:
    raw_obs = payload.get("observations", [])
    if not isinstance(raw_obs, list):
        _fail(ErrorCode.MALFORMED_PAYLOAD, "'observations' must be a list", raw_obs)
    observations: list[ObservationRecord] = []
    for i, raw in enumerate(raw_obs):
        context = f"observations[{i}]"
        if not isinstance(raw, dict):
            _fail(ErrorCode.MALFORMED_PAYLOAD, f"observation must be an object in {context}", raw)
        coding = _normalize_coding(_require(raw, "code", context), context)
        if source_version == "fhir-r4":
            value = _require(raw, "value", context)
        else:  # fhir-r5 allows valueString / valueQuantity.value
            value = raw.get("valueString")
            if value is None:
                quantity = raw.get("valueQuantity")
                if isinstance(quantity, dict) and quantity.get("value") is not None:
                    value = quantity["value"]
            if value is None:
                _fail(ErrorCode.MISSING_REQUIRED_FIELD,
                      f"observation value missing in {context}", raw)
        unit = raw.get("unit")
        quantity = raw.get("valueQuantity")
        if unit is None and isinstance(quantity, dict):
            unit = quantity.get("unit")
        observations.append(ObservationRecord(
            code=coding,
            value=str(value),
            unit=unit,
            effective=raw.get("effective"),
        ))
    return observations


def normalize(payload: dict, source_version: str) -> PatientResource:
    """Normalize an external payload into the unified internal model."""
    if source_version not in SUPPORTED_VERSIONS:
        _fail(
            ErrorCode.UNSUPPORTED_VERSION,
            f"source version '{source_version}' is not supported",
            {"source_version": source_version, "supported": sorted(SUPPORTED_VERSIONS)},
        )
    if not isinstance(payload, dict):
        _fail(ErrorCode.MALFORMED_PAYLOAD, "payload must be a JSON object", payload)
    patient_id = _require(payload, "patient_id", "patient")
    return PatientResource(
        patient_id=str(patient_id),
        active=bool(payload.get("active", True)),
        identifiers=_normalize_identifiers(payload, source_version),
        observations=_normalize_observations(payload, source_version),
    )
