"""Host-owned verifier admission and execution registry.

Verifier handlers may report bounded facts, but only this registry can attach an
admitted proof level to those facts.  The proof calculator subsequently revalidates
the admission metadata against the live registration, so declared verifier names and
plugin-provided output fields cannot raise proof strength on their own.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
import secrets
import threading
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass, is_dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match

from ..runtime.registrations import RegistrationDisposer, RegistrationToken
from .proof_levels import normalize_proof_level


_PROOF_RANK = {level: index for index, level in enumerate(("P0", "P1", "P2", "P3", "P4"))}
_VALID_STATUSES = frozenset({"pass", "fail", "uncertain", "error"})
_VERIFIER_ID_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9_-]{0,63}(?:\.[a-z0-9][a-z0-9_-]{0,63})+$"
)
_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_PLUGIN_WORKER_COUNT = 4
_PLUGIN_EXECUTOR = ThreadPoolExecutor(
    max_workers=_PLUGIN_WORKER_COUNT,
    thread_name_prefix="tcode-verifier",
)
_PLUGIN_EXECUTION_SLOTS = threading.BoundedSemaphore(_PLUGIN_WORKER_COUNT)


VERIFIER_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["candidate", "criterion", "contract", "evidence"],
    "properties": {
        "candidate": {"type": "string"},
        "criterion": {"type": "object"},
        "contract": {"type": "object"},
        "evidence": {"type": "array", "items": {"type": "object"}},
        "workspace_root": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}

VERIFIER_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["status", "reason_code"],
    "properties": {
        "status": {"enum": ["pass", "fail", "uncertain", "error"]},
        "reason_code": {"type": "string", "minLength": 1, "maxLength": 200},
        "observations": {
            "type": "array",
            "items": {"type": "string", "maxLength": 200},
            "maxItems": 64,
        },
        "artifact_refs": {
            "type": "array",
            "items": {"type": "object"},
            "maxItems": 64,
        },
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "maxLength": 300},
            "maxItems": 128,
        },
        "details": {"type": "object"},
        "retryable": {"type": "boolean"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class VerifierDefinition:
    """A verifier definition after Host admission."""

    verifier_id: str
    verifier_version: str
    provider: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    admitted_max_proof_level: str
    timeout_seconds: float
    required_capabilities: tuple[str, ...]
    isolation_policy: str
    aliases: tuple[str, ...] = ()
    description: str = ""

    def projection(self) -> dict[str, Any]:
        return {
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "provider": self.provider,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "admitted_max_proof_level": self.admitted_max_proof_level,
            "timeout_seconds": self.timeout_seconds,
            "required_capabilities": list(self.required_capabilities),
            "isolation_policy": self.isolation_policy,
            "aliases": list(self.aliases),
            "description": self.description,
        }

    def digest(self) -> str:
        encoded = json.dumps(
            self.projection(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="ignore")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class _RegisteredVerifier:
    definition: VerifierDefinition
    handler: Callable[..., Any]
    token: RegistrationToken
    source: str
    instance_id: str
    receipt_key: bytes


class VerifierRegistrationError(ValueError):
    """Raised when a verifier cannot be admitted safely."""


def _schema_error(schema: Mapping[str, Any], *, label: str) -> Optional[str]:
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        return f"invalid {label}: {exc.message}"
    if schema.get("type") != "object":
        return f"{label} root type must be object"
    return None


def _bounded_mapping(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, Mapping):
        return {
            str(key)[:120]: _bounded_mapping(item, depth=depth + 1)
            for key, item in list(value.items())[:128]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_bounded_mapping(item, depth=depth + 1) for item in list(value)[:128]]
    return f"<{type(value).__name__}>"


def _input_binding(input_payload: Mapping[str, Any]) -> dict[str, Any]:
    criterion = input_payload.get("criterion")
    contract = input_payload.get("contract")
    criterion_map = criterion if isinstance(criterion, Mapping) else {}
    contract_map = contract if isinstance(contract, Mapping) else {}
    encoded = json.dumps(
        dict(input_payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="ignore")
    return {
        "contract_id": str(contract_map.get("contract_id") or ""),
        "contract_version": int(contract_map.get("version") or 0),
        "criterion_id": str(criterion_map.get("criterion_id") or ""),
        "input_digest": hashlib.sha256(encoded).hexdigest(),
    }


def verifier_input_digest(input_payload: Mapping[str, Any]) -> str:
    """Return the canonical digest used to bind a result to verifier input."""

    return str(_input_binding(input_payload)["input_digest"])


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="ignore")


def _admission_receipt(record: _RegisteredVerifier, result: Mapping[str, Any]) -> str:
    unsigned = dict(result)
    unsigned.pop("admission_receipt", None)
    return hmac.new(
        record.receipt_key,
        _canonical_bytes(unsigned),
        hashlib.sha256,
    ).hexdigest()


def _run_plugin_handler(
    handler: Callable[..., Any],
    input_payload: Mapping[str, Any],
) -> Any:
    try:
        return handler(input_payload)
    finally:
        _PLUGIN_EXECUTION_SLOTS.release()


def _error_result(
    *,
    verifier_ref: str,
    reason_code: str,
    details: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "requested_verifier_ref": verifier_ref,
        "status": "error" if reason_code != "verifier_not_registered" else "uncertain",
        "reason_code": reason_code,
        "observations": [reason_code],
        "artifact_refs": [],
        "evidence_refs": [],
        "details": dict(_bounded_mapping(details or {})),
        "retryable": reason_code
        in {
            "verifier_timeout",
            "verifier_execution_error",
            "verifier_execution_capacity_exhausted",
        },
        "registry_verified": False,
        "admitted_proof_level": "P0",
    }


from ..runtime.workspace_registry import WorkspaceRegistryMeta


class VerifierRegistry(metaclass=WorkspaceRegistryMeta):

    """Process-wide, instance-owned verifier registry."""
    _workspace_fields = ('_records', '_aliases')

    _records: dict[str, _RegisteredVerifier] = {}
    _aliases: dict[str, str] = {}
    _lock = threading.RLock()

    @classmethod
    def register(
        cls,
        definition: VerifierDefinition,
        handler: Callable[..., Any],
        *,
        source: str,
        instance_id: str = "",
        replace: bool = False,
    ) -> RegistrationDisposer:
        verifier_id = str(definition.verifier_id or "").strip().lower()
        if not _VERIFIER_ID_PATTERN.fullmatch(verifier_id):
            raise VerifierRegistrationError(
                "verifier_id must be a lowercase dotted identifier"
            )
        if not _VERSION_PATTERN.fullmatch(str(definition.verifier_version or "")):
            raise VerifierRegistrationError(
                "verifier_version must be a semantic version"
            )
        if not callable(handler):
            raise VerifierRegistrationError("verifier handler must be callable")
        if definition.provider not in {"core", "plugin"}:
            raise VerifierRegistrationError("provider must be core or plugin")
        if normalize_proof_level(definition.admitted_max_proof_level, default="") == "":
            raise VerifierRegistrationError("admitted_max_proof_level must be P0..P4")
        for label, schema in (
            ("verifier input schema", definition.input_schema),
            ("verifier output schema", definition.output_schema),
        ):
            if not isinstance(schema, Mapping):
                raise VerifierRegistrationError(f"{label} must be a mapping")
            error = _schema_error(schema, label=label)
            if error:
                raise VerifierRegistrationError(error)
        aliases = tuple(
            dict.fromkeys(
                str(item or "").strip().lower()
                for item in definition.aliases
                if str(item or "").strip()
            )
        )
        normalized = VerifierDefinition(
            verifier_id=verifier_id,
            verifier_version=str(definition.verifier_version or "").strip(),
            provider=definition.provider,
            input_schema=dict(definition.input_schema),
            output_schema=dict(definition.output_schema),
            admitted_max_proof_level=normalize_proof_level(
                definition.admitted_max_proof_level
            ),
            timeout_seconds=max(0.01, min(float(definition.timeout_seconds), 300.0)),
            required_capabilities=tuple(definition.required_capabilities),
            isolation_policy=str(definition.isolation_policy or "").strip(),
            aliases=aliases,
            description=str(definition.description or "").strip(),
        )
        with cls._lock:
            keys = (verifier_id, *aliases)
            conflicts = sorted(
                key
                for key in keys
                if (key in cls._records or key in cls._aliases)
                and not (replace and cls._aliases.get(key, key) == verifier_id)
            )
            if conflicts:
                raise VerifierRegistrationError(
                    "verifier id or alias already registered: "
                    + ", ".join(conflicts)
                )
            if replace and verifier_id in cls._records:
                cls._remove_record(verifier_id)
            token = RegistrationToken.create(
                registry="verifiers",
                key=verifier_id,
                source=source,
                instance_id=instance_id,
            )
            cls._records[verifier_id] = _RegisteredVerifier(
                definition=normalized,
                handler=handler,
                token=token,
                source=str(source or "").strip(),
                instance_id=str(instance_id or "").strip(),
                receipt_key=secrets.token_bytes(32),
            )
            for alias in aliases:
                cls._aliases[alias] = verifier_id
        return RegistrationDisposer(token, cls.unregister_exact)

    @classmethod
    def register_plugin(
        cls,
        contribution: Any,
        *,
        source: str,
        instance_id: str,
        granted_capabilities: tuple[str, ...] = (),
    ) -> RegistrationDisposer:
        verifier_id = str(getattr(contribution, "verifier_id", "") or "").strip().lower()
        namespace = f"{str(source or '').strip().lower()}."
        if not verifier_id.startswith(namespace):
            raise VerifierRegistrationError(
                f"plugin verifier_id must use the {namespace!r} namespace"
            )
        requested = normalize_proof_level(
            getattr(contribution, "requested_max_proof_level", "P1")
        )
        # Controlled-host plugin execution remains experimental in v1. Plugins can
        # contribute useful facts, but cannot receive deterministic P2 authority
        # until the isolated verifier RPC and durable receipt contract exist.
        admitted = min(_PROOF_RANK[requested], _PROOF_RANK["P1"])
        required_capabilities = tuple(
            str(item or "").strip()
            for item in getattr(contribution, "required_capabilities", ())
            if str(item or "").strip()
        )
        missing = sorted(set(required_capabilities) - set(granted_capabilities))
        if missing:
            raise VerifierRegistrationError(
                "verifier requires ungranted capabilities: " + ", ".join(missing)
            )
        isolation_policy = str(
            getattr(contribution, "isolation_policy", "controlled-host")
            or "controlled-host"
        ).strip()
        if isolation_policy != "controlled-host":
            raise VerifierRegistrationError(
                "Plugin SDK verifier isolation_policy must be controlled-host"
            )
        definition = VerifierDefinition(
            verifier_id=verifier_id,
            verifier_version=str(getattr(contribution, "version", "") or "").strip(),
            provider="plugin",
            input_schema=dict(getattr(contribution, "input_schema", {}) or {}),
            output_schema=dict(getattr(contribution, "output_schema", {}) or {}),
            admitted_max_proof_level=("P0", "P1", "P2", "P3", "P4")[admitted],
            timeout_seconds=float(getattr(contribution, "timeout_seconds", 5.0)),
            required_capabilities=required_capabilities,
            isolation_policy=isolation_policy,
            description=str(getattr(contribution, "description", "") or ""),
        )
        return cls.register(
            definition,
            getattr(contribution, "handler", None),
            source=source,
            instance_id=instance_id,
            replace=False,
        )

    @classmethod
    def resolve(cls, verifier_ref: str) -> Optional[_RegisteredVerifier]:
        normalized = str(verifier_ref or "").strip().lower()
        with cls._lock:
            canonical = cls._aliases.get(normalized, normalized)
            return cls._records.get(canonical)

    @classmethod
    def definitions(cls) -> list[VerifierDefinition]:
        with cls._lock:
            return [cls._records[key].definition for key in sorted(cls._records)]

    @classmethod
    def execute(
        cls,
        verifier_ref: str,
        input_payload: Mapping[str, Any],
        *,
        execution_context: Any = None,
    ) -> dict[str, Any]:
        record = cls.resolve(verifier_ref)
        if record is None:
            return _error_result(
                verifier_ref=verifier_ref,
                reason_code="verifier_not_registered",
                details={"verifier_ref": verifier_ref},
            )
        input_errors = list(
            Draft202012Validator(dict(record.definition.input_schema)).iter_errors(
                dict(input_payload)
            )
        )
        if input_errors:
            selected = best_match(input_errors) or input_errors[0]
            return cls._wrap_error(
                record,
                verifier_ref,
                "verifier_input_invalid",
                {"message": selected.message},
            )
        binding = _input_binding(input_payload)
        try:
            if record.definition.provider == "plugin":
                if not _PLUGIN_EXECUTION_SLOTS.acquire(blocking=False):
                    return cls._wrap_error(
                        record,
                        verifier_ref,
                        "verifier_execution_capacity_exhausted",
                        {"worker_limit": _PLUGIN_WORKER_COUNT},
                    )
                future: Future[Any]
                try:
                    future = _PLUGIN_EXECUTOR.submit(
                        _run_plugin_handler,
                        record.handler,
                        MappingProxyType(dict(input_payload)),
                    )
                except BaseException:
                    _PLUGIN_EXECUTION_SLOTS.release()
                    raise
                try:
                    raw = future.result(timeout=record.definition.timeout_seconds)
                except FutureTimeoutError:
                    # A running Python thread cannot be force-killed. Cancellation
                    # releases the slot only when execution has not started; otherwise
                    # the worker releases it when the trusted handler eventually exits.
                    if future.cancel():
                        _PLUGIN_EXECUTION_SLOTS.release()
                    cls.unregister_exact(record.token)
                    return cls._wrap_error(
                        record,
                        verifier_ref,
                        "verifier_timeout",
                        {
                            "timeout_seconds": record.definition.timeout_seconds,
                            "registration_disabled": True,
                            "execution_isolation": "bounded-controlled-host",
                        },
                    )
            else:
                raw = record.handler(
                    MappingProxyType(dict(input_payload)),
                    execution_context,
                )
            if inspect.isawaitable(raw):
                close = getattr(raw, "close", None)
                if callable(close):
                    close()
                return cls._wrap_error(
                    record,
                    verifier_ref,
                    "verifier_async_handler_unsupported",
                )
        except BaseException as exc:
            return cls._wrap_error(
                record,
                verifier_ref,
                "verifier_execution_error",
                {"error_type": type(exc).__name__, "message": str(exc)[:500]},
            )
        if is_dataclass(raw):
            raw = asdict(raw)
        if not isinstance(raw, Mapping):
            return cls._wrap_error(
                record,
                verifier_ref,
                "verifier_output_invalid",
                {"message": "handler must return VerifierOutput or a mapping"},
            )
        normalized_raw = _bounded_mapping(raw)
        assert isinstance(normalized_raw, dict)
        return cls._admit_payload(
            record,
            verifier_ref,
            normalized_raw,
            binding=binding,
        )

    @classmethod
    def attest_core_result(
        cls,
        verifier_ref: str,
        payload: Mapping[str, Any],
        *,
        binding: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Attach registry admission to a fact produced by trusted Host code."""

        record = cls.resolve(verifier_ref)
        if record is None or record.definition.provider != "core":
            return _error_result(
                verifier_ref=verifier_ref,
                reason_code="verifier_not_registered",
            )
        binding_map = binding if isinstance(binding, Mapping) else {}
        binding_verifier_id = str(binding_map.get("verifier_id") or "").strip().lower()
        binding_record = cls.resolve(binding_verifier_id)
        try:
            binding_contract_version = int(binding_map.get("contract_version") or 0)
        except (TypeError, ValueError):
            binding_contract_version = 0
        if (
            binding_record is None
            or binding_record.definition.verifier_id != record.definition.verifier_id
            or not cls.is_authentic_result(
                binding_map,
                contract_id=str(binding_map.get("contract_id") or ""),
                contract_version=binding_contract_version,
                criterion_id=str(binding_map.get("criterion_id") or ""),
                input_digest=str(binding_map.get("input_digest") or ""),
            )
        ):
            return cls._wrap_error(
                record,
                verifier_ref,
                "verifier_attestation_binding_invalid",
            )
        return cls._admit_payload(
            record,
            verifier_ref,
            dict(payload),
            binding=binding_map,
        )

    @classmethod
    def _admit_payload(
        cls,
        record: _RegisteredVerifier,
        verifier_ref: str,
        payload: dict[str, Any],
        *,
        binding: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        core_achieved = payload.pop("_achieved_proof_level", None)
        for schema in (VERIFIER_OUTPUT_SCHEMA, record.definition.output_schema):
            errors = list(Draft202012Validator(dict(schema)).iter_errors(payload))
            if errors:
                selected = best_match(errors) or errors[0]
                return cls._wrap_error(
                    record,
                    verifier_ref,
                    "verifier_output_schema_invalid",
                    {"message": selected.message},
                )
        status = str(payload.get("status") or "error").strip().lower()
        if status not in _VALID_STATUSES:
            status = "error"
        admitted = "P0"
        if status == "pass":
            admitted = record.definition.admitted_max_proof_level
            if record.definition.provider == "core" and core_achieved is not None:
                requested_rank = _PROOF_RANK[
                    normalize_proof_level(core_achieved, default="P0")
                ]
                admitted = ("P0", "P1", "P2", "P3", "P4")[
                    min(requested_rank, _PROOF_RANK[admitted])
                ]
        observations = [
            str(item or "").strip()[:200]
            for item in payload.get("observations") or []
            if str(item or "").strip()
        ]
        reason_code = str(payload.get("reason_code") or "verifier_result").strip()[:200]
        if not observations:
            observations = [reason_code]
        admitted_result = {
            "requested_verifier_ref": str(verifier_ref or "").strip(),
            "verifier_id": record.definition.verifier_id,
            "verifier_version": record.definition.verifier_version,
            "provider": record.definition.provider,
            "source": record.source,
            "plugin_instance_id": record.instance_id,
            "definition_digest": record.definition.digest(),
            "admission_id": record.token.registration_id,
            "status": status,
            "reason_code": reason_code,
            "observations": observations,
            "artifact_refs": list(_bounded_mapping(payload.get("artifact_refs") or [])),
            "evidence_refs": [
                str(item or "").strip()[:300]
                for item in payload.get("evidence_refs") or []
                if str(item or "").strip()
            ],
            "details": dict(_bounded_mapping(payload.get("details") or {})),
            "retryable": bool(payload.get("retryable", False)),
            "registry_verified": True,
            "admitted_proof_level": admitted,
        }
        for key in ("contract_id", "contract_version", "criterion_id", "input_digest"):
            if binding is not None and binding.get(key) not in {None, ""}:
                admitted_result[key] = binding.get(key)
        required_binding = {
            "contract_id",
            "contract_version",
            "criterion_id",
            "input_digest",
        }
        if status == "pass" and not required_binding.issubset(admitted_result):
            return cls._wrap_error(
                record,
                verifier_ref,
                "verifier_result_binding_missing",
            )
        admitted_result["admission_receipt"] = _admission_receipt(
            record,
            admitted_result,
        )
        return admitted_result

    @classmethod
    def _wrap_error(
        cls,
        record: _RegisteredVerifier,
        verifier_ref: str,
        reason_code: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        result = _error_result(
            verifier_ref=verifier_ref,
            reason_code=reason_code,
            details=details,
        )
        result.update(
            {
                "verifier_id": record.definition.verifier_id,
                "verifier_version": record.definition.verifier_version,
                "provider": record.definition.provider,
                "source": record.source,
                "plugin_instance_id": record.instance_id,
                "definition_digest": record.definition.digest(),
                "admission_id": record.token.registration_id,
            }
        )
        return result

    @classmethod
    def is_admitted_result(
        cls,
        result: Mapping[str, Any],
        *,
        contract_id: str = "",
        contract_version: int = 0,
        criterion_id: str = "",
        input_digest: str = "",
    ) -> bool:
        if not cls.is_authentic_result(
            result,
            contract_id=contract_id,
            contract_version=contract_version,
            criterion_id=criterion_id,
            input_digest=input_digest,
        ):
            return False
        with cls._lock:
            verifier_id = str(result.get("verifier_id") or "").strip().lower()
            canonical = cls._aliases.get(verifier_id, verifier_id)
            record = cls._records.get(canonical)
            if record is None or str(result.get("status") or "") != "pass":
                return False
            admitted = normalize_proof_level(result.get("admitted_proof_level"))
            return _PROOF_RANK[admitted] <= _PROOF_RANK[
                record.definition.admitted_max_proof_level
            ]

    @classmethod
    def is_authentic_result(
        cls,
        result: Mapping[str, Any],
        *,
        contract_id: str = "",
        contract_version: int = 0,
        criterion_id: str = "",
        input_digest: str = "",
    ) -> bool:
        """Validate registry identity, input binding and the runtime HMAC receipt."""

        if not bool(result.get("registry_verified")):
            return False
        with cls._lock:
            verifier_id = str(result.get("verifier_id") or "").strip().lower()
            canonical = cls._aliases.get(verifier_id, verifier_id)
            record = cls._records.get(canonical)
            if record is None:
                return False
            definition = record.definition
            if str(result.get("verifier_version") or "") != definition.verifier_version:
                return False
            if str(result.get("definition_digest") or "") != definition.digest():
                return False
            if str(result.get("admission_id") or "") != record.token.registration_id:
                return False
            if str(result.get("source") or "") != record.source:
                return False
            if str(result.get("plugin_instance_id") or "") != record.instance_id:
                return False
            receipt = str(result.get("admission_receipt") or "")
            if not receipt or not hmac.compare_digest(
                receipt,
                _admission_receipt(record, result),
            ):
                return False
            if contract_id and str(result.get("contract_id") or "") != contract_id:
                return False
            if contract_version:
                try:
                    result_contract_version = int(
                        result.get("contract_version") or 0
                    )
                except (TypeError, ValueError):
                    return False
                if result_contract_version != int(contract_version):
                    return False
            if criterion_id and str(result.get("criterion_id") or "") != criterion_id:
                return False
            if not input_digest:
                return False
            if str(result.get("input_digest") or "") != str(input_digest):
                return False
            return True

    @classmethod
    def unregister_exact(cls, token: RegistrationToken) -> bool:
        with cls._lock:
            record = cls._records.get(token.key)
            if record is None or record.token != token:
                return False
            cls._remove_record(token.key)
            return True

    @classmethod
    def _remove_record(cls, verifier_id: str) -> None:
        with cls._lock:
            record = cls._records.pop(verifier_id, None)
            if record is None:
                return
            for alias in record.definition.aliases:
                if cls._aliases.get(alias) == verifier_id:
                    cls._aliases.pop(alias, None)

    @classmethod
    def unregister_by_instance(cls, instance_id: str) -> list[str]:
        with cls._lock:
            removed = [
                verifier_id
                for verifier_id, record in cls._records.items()
                if record.instance_id and record.instance_id == instance_id
            ]
            for verifier_id in removed:
                cls._remove_record(verifier_id)
            return sorted(removed)

    @classmethod
    def unregister_by_source(
        cls,
        source: str,
        *,
        legacy_only: bool = False,
    ) -> list[str]:
        with cls._lock:
            removed = [
                verifier_id
                for verifier_id, record in cls._records.items()
                if record.definition.provider == "plugin"
                and record.source == source
                and (not legacy_only or not record.instance_id)
            ]
            for verifier_id in removed:
                cls._remove_record(verifier_id)
            return sorted(removed)

    @classmethod
    def clear_plugins(cls) -> None:
        with cls._lock:
            for verifier_id in [
                key
                for key, record in cls._records.items()
                if record.definition.provider == "plugin"
            ]:
                cls._remove_record(verifier_id)
