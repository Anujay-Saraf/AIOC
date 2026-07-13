from __future__ import annotations

import os
from typing import Any, Dict

import httpx
from fastapi import Header, HTTPException


async def require_api_access(authorization: str = Header(default="")) -> None:
    jwks_url = os.getenv("KEYCLOAK_JWKS_URL", "").strip()
    if jwks_url:
        _verify_keycloak_token(authorization, jwks_url)
        return
    token = os.getenv("API_AUTH_TOKEN", "").strip()
    if not token:
        return
    expected = f"Bearer {token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid API token")


def _verify_keycloak_token(authorization: str, jwks_url: str) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    raw_token = authorization.removeprefix("Bearer ").strip()
    audience = os.getenv("KEYCLOAK_AUDIENCE", "").strip()
    try:
        import jwt
        from jwt import PyJWKClient

        signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(raw_token)
        options = {"verify_aud": bool(audience)}
        jwt.decode(
            raw_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience or None,
            options=options,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"invalid Keycloak token: {exc}") from exc


async def authorize_review(action: str, record: Dict[str, Any], body: Dict[str, Any]) -> None:
    opa_url = os.getenv("OPA_URL", "").rstrip("/")
    if not opa_url:
        return
    input_payload = {
        "input": {
            "action": action,
            "actor": body.get("actor", "anonymous"),
            "incident": {
                "incident_id": record.get("incident_id"),
                "service": record.get("service"),
                "severity": record.get("severity"),
                "lifecycle_status": record.get("lifecycle_status"),
            },
        }
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{opa_url}/v1/data/incident_response/allow",
                json=input_payload,
            )
            response.raise_for_status()
        allowed = bool(response.json().get("result"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"OPA authorization failed: {exc}") from exc
    if not allowed:
        raise HTTPException(status_code=403, detail="review action denied by policy")


def evaluate_remediation_policy(record: Dict[str, Any], step_index: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Local policy guardrail for remediation approvals.

    OPA can still be used for central authorization, but this built-in policy
    prevents unsafe approvals in offline/local demos: rollback readiness, blast
    radius, approval actor, and explicit safety checks are recorded for audit.
    """
    recommendations = record.get("recovery_recommendations") or []
    step = str(recommendations[step_index]) if 0 <= step_index < len(recommendations) else ""
    decision = str(body.get("decision", ""))
    actor = str(body.get("actor", "anonymous")).strip() or "anonymous"
    rollback = record.get("rollback_plan") or {}
    blast = record.get("blast_radius") or {}
    requires_human = decision == "approved"
    checks = []
    if requires_human and actor == "anonymous":
        checks.append({"check": "named_approver", "passed": False, "reason": "approved remediation requires a named human actor"})
    else:
        checks.append({"check": "named_approver", "passed": True})
    risk_level = classify_remediation_risk(step, record)
    high_risk = risk_level == "high" or blast.get("estimated_scope") == "high"
    if requires_human and high_risk and not rollback.get("strategy"):
        checks.append({"check": "rollback_strategy", "passed": False, "reason": "high-risk remediation needs rollback_plan.strategy"})
    else:
        checks.append({"check": "rollback_strategy", "passed": True})
    if requires_human and high_risk and not rollback.get("safety_check"):
        checks.append({"check": "safety_check", "passed": False, "reason": "high-risk remediation needs rollback_plan.safety_check"})
    else:
        checks.append({"check": "safety_check", "passed": True})
    allowed = all(item.get("passed") for item in checks)
    return {
        "allowed": allowed,
        "requires_human_approval": requires_human,
        "risk_level": risk_level,
        "high_risk_action": high_risk,
        "step_index": step_index,
        "checks": checks,
        "policy_version": "local-remediation-v1",
    }


def classify_remediation_risk(step: str, record: Dict[str, Any]) -> str:
    blast = record.get("blast_radius") or {}
    severity = str(record.get("severity", "")).casefold()
    step_text = str(step or "").casefold()
    risky_terms = (
        "rollback",
        "restart",
        "failover",
        "disable",
        "traffic",
        "scale",
        "drain",
        "delete",
        "reconfigure",
        "patch",
        "reboot",
    )
    if blast.get("estimated_scope") == "high":
        return "high"
    if any(term in step_text for term in risky_terms):
        return "high"
    if severity in {"critical", "sev0", "sev1", "sev2"}:
        return "high"
    return "standard"
