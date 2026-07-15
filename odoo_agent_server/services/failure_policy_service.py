from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal


FailureClass = Literal[
    "deployment_waiting",
    "transient",
    "code_validation",
    "compatibility",
    "configuration",
    "authorization",
    "registry",
    "functional",
    "unknown",
]


@dataclass(frozen=True)
class FailureDecision:
    failure_class: FailureClass
    fingerprint: str
    retryable: bool
    correction_required: bool
    human_action_required: bool
    circuit_open: bool
    max_attempts: int
    retry_after_seconds: int | None
    action: str
    hints: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["hints"] = list(self.hints)
        return payload


class FailurePolicyService:
    """Classify failures before retrying or asking an agent for a correction.

    This service never edits code. It converts noisy command/log output into a
    stable retry contract so workflows do not blindly replay deterministic
    failures or describe a bounded deployment wait as a failed deployment.
    """

    _WAITING = (
        "deployment_waiting", "still running", "in progress", "queued",
        "not deployed yet", "retry_later", "bounded confirmation window",
    )
    _TRANSIENT = (
        "timed out", "timeout", "connection reset", "connection refused",
        "temporarily unavailable", "temporary failure", "bad gateway",
        "service unavailable", "rate limit", "too many requests", "ssh_exchange_identification",
    )
    _AUTH = (
        "permission denied", "access denied", "forbidden", "unauthorized",
        "invalid token", "authentication failed", "protected branch",
    )
    _CONFIG = (
        "not configured", "missing configuration", "missing required",
        "unknown database", "repository not found", "host key verification failed",
        "no such file or directory", "invalid branch",
    )
    _REGISTRY = (
        "registry", "field does not exist", "missing expected fields",
        "keyerror", "model not found", "external id not found",
    )
    _COMPATIBILITY = (
        "xpath", "cannot be located in parent view", "owl", "qweb",
        "deprecated", "attributeerror", "unexpected keyword argument",
    )
    _CODE = (
        "syntaxerror", "indentationerror", "importerror", "modulenotfounderror",
        "xmlsyntaxerror", "parseerror", "assertionerror", "failed to load",
    )
    _FUNCTIONAL = (
        "functional verification", "browser verification", "console error",
        "expected result", "smoke test", "user-visible", "not fixed",
    )

    def decide(
        self,
        *,
        stage: str,
        error: str | None,
        attempt: int = 1,
        max_attempts: int | None = None,
    ) -> FailureDecision:
        text = " ".join((error or "unknown failure").lower().split())
        failure_class = self._classify(stage.lower(), text)
        policy_max = max_attempts or self._default_attempts(failure_class)
        policy_max = max(1, int(policy_max))
        attempt = max(1, int(attempt))

        retryable = failure_class in {"deployment_waiting", "transient"}
        correction_required = failure_class in {
            "code_validation", "compatibility", "registry", "functional", "unknown",
        }
        human_required = failure_class in {"configuration", "authorization"}
        circuit_open = attempt >= policy_max and (retryable or correction_required)
        if circuit_open:
            retryable = False

        return FailureDecision(
            failure_class=failure_class,
            fingerprint=self.fingerprint(stage=stage, error=text),
            retryable=retryable,
            correction_required=correction_required,
            human_action_required=human_required,
            circuit_open=circuit_open,
            max_attempts=policy_max,
            retry_after_seconds=self._backoff(attempt) if retryable else None,
            action=self._action(failure_class, circuit_open),
            hints=self._hints(failure_class),
        )

    def fingerprint(self, *, stage: str, error: str | None) -> str:
        normalized = (error or "unknown failure").lower()
        normalized = re.sub(r"\b[0-9a-f]{7,64}\b", "<sha>", normalized)
        normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", normalized)
        normalized = re.sub(r"[A-Za-z]:\\[^\s]+|/(?:[^\s/]+/)+[^\s]+", "<path>", normalized)
        normalized = " ".join(normalized.split())[:2000]
        raw = f"{stage.strip().lower()}|{normalized}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _classify(self, stage: str, text: str) -> FailureClass:
        if any(marker in text for marker in self._WAITING):
            return "deployment_waiting"
        if any(marker in text for marker in self._AUTH):
            return "authorization"
        if any(marker in text for marker in self._CONFIG):
            return "configuration"
        if any(marker in text for marker in self._TRANSIENT):
            return "transient"
        if "registry" in stage or any(marker in text for marker in self._REGISTRY):
            return "registry"
        if any(marker in text for marker in self._COMPATIBILITY):
            return "compatibility"
        if any(marker in text for marker in self._CODE):
            return "code_validation"
        if "browser" in stage or "functional" in stage or any(marker in text for marker in self._FUNCTIONAL):
            return "functional"
        return "unknown"

    @staticmethod
    def _default_attempts(failure_class: FailureClass) -> int:
        if failure_class in {"deployment_waiting", "transient"}:
            return 3
        if failure_class in {"code_validation", "compatibility", "registry", "functional", "unknown"}:
            return 2
        return 1

    @staticmethod
    def _backoff(attempt: int) -> int:
        return (2, 5, 10)[min(max(attempt - 1, 0), 2)]

    @staticmethod
    def _action(failure_class: FailureClass, circuit_open: bool) -> str:
        if circuit_open:
            return "stop_and_report_evidence"
        if failure_class == "deployment_waiting":
            return "resume_from_checkpoint"
        if failure_class == "transient":
            return "retry_same_step"
        if failure_class in {"code_validation", "compatibility", "registry", "functional", "unknown"}:
            return "inspect_and_submit_minimal_correction"
        if failure_class == "authorization":
            return "request_authorization"
        return "fix_configuration"

    @staticmethod
    def _hints(failure_class: FailureClass) -> tuple[str, ...]:
        hints: dict[FailureClass, tuple[str, ...]] = {
            "deployment_waiting": ("Resume the stored job; do not repeat commit or push.",),
            "transient": ("Retry only the failed external step with bounded backoff.",),
            "code_validation": ("Inspect the implicated changed files and correct the exact parser/import failure locally before deploying again.",),
            "compatibility": ("Re-read the matching installed Odoo source for the target version before changing the patch.",),
            "configuration": ("Correct the reported client/repository/Odoo.sh setting; retrying unchanged configuration will not help.",),
            "authorization": ("Obtain the required scoped credential or approval; do not bypass the guard.",),
            "registry": ("Compare expected models/fields/XML IDs with the deployed SHA and module upgrade result.",),
            "functional": ("Trace the real entry point to output and compare expected runtime state with observed state.",),
            "unknown": ("Collect a scoped traceback/error pack and form a new evidence-based hypothesis before another write.",),
        }
        return hints[failure_class]
