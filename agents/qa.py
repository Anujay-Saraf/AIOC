from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Tuple


from agents.knowledge_base import build_knowledge_context
from agents.llm import (
    complete_json,
    complete_local_json,
    get_model,
    get_ollama_config,
    get_provider,
    llm_available,
)
from agents.query_memory import context_fingerprint, lookup_answer, remember_answer, upsert_knowledge_evidence

ASSISTANT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
        "follow_ups": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "source_path": {"type": "string"},
                    "kind": {"type": "string"},
                    "content": {"type": "string"},
                    "score": {"type": "number"},
                    "citation": {"type": "string"},
                },
                "required": ["title", "source_path", "kind", "content", "score", "citation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answer", "confidence", "follow_ups", "language", "citations"],
    "additionalProperties": False,
}

LOCAL_ASSISTANT_SCHEMA: Dict[str, Any] = json.loads(json.dumps(ASSISTANT_SCHEMA))
LOCAL_ASSISTANT_SCHEMA["properties"].update(
    {
        "answerable": {"type": "boolean"},
        "fallback_reason": {"type": "string"},
    }
)
LOCAL_ASSISTANT_SCHEMA["required"].extend(["answerable", "fallback_reason"])

CONTEXT_KEYS = (
    "service",
    "severity",
    "alert_description",
    "current_status",
    "root_cause",
    "log_anomalies",
    "log_context_cache",
    "metric_anomalies",
    "deployment_changes",
    "affected_users",
    "estimated_revenue_impact_per_minute",
    "estimated_cost_impact_per_minute",
    "revenue_impact_justification",
    "recovery_recommendations",
    "troubleshooting_plan",
    "stakeholder_updates",
    "similar_incidents",
    "ownership",
    "environment",
    "dependencies",
    "upstream_services",
    "runbooks",
    "escalation_path",
    "rollback_plan",
    "blast_radius",
)


async def answer_question(
    record: Dict[str, Any],
    question: str,
    *,
    language_code: str | None = None,
    system_context: Dict[str, Any] | None = None,
) -> Tuple[Dict[str, Any], str]:
    """Answer through cache -> local Qwen -> selected online LLM -> heuristic.

    ``system_context`` is a bounded platform snapshot used by the global Jarvis
    assistant. Incident-scoped callers keep passing only ``record``.
    """
    language = language_code or "en"
    knowledge_context = build_knowledge_context(
        question,
        incident_context=record if record else None,
    )
    upsert_knowledge_evidence(question, knowledge_context, record=record if record else None)
    fingerprint = context_fingerprint(record, knowledge_context, system_context)
    cached = lookup_answer(question, fingerprint, language)
    if cached:
        payload = dict(cached.payload)
        payload["routing"] = _route_metadata(
            cache_hit=True,
            tier="cache",
            provider=cached.provider,
            model=cached.model,
            fallback_reason=None,
            context_fingerprint=fingerprint,
            cache_match=cached.match,
            cache_similarity=cached.similarity,
        )
        return payload, "cache:graph-query-memory"

    # Dynamic Graph RAG: only run Cypher gen when graph DB is live, else fast fallback
    graph_context = await execute_graph_rag(question)

    prompt = _assistant_prompt(record, knowledge_context, question, language, system_context, graph_context)
    fallback_reasons: List[str] = []
    local = get_ollama_config()
    import asyncio
    if local.enabled:
        try:
            result = await asyncio.wait_for(
                complete_local_json(
                    system=(
                        "You are the local first-line incident assistant. Use only the supplied "
                        "incident, platform, and knowledge context. Mark answerable=false when the "
                        "evidence is incomplete or you are not confident; never guess."
                    ),
                    prompt=prompt,
                    schema=LOCAL_ASSISTANT_SCHEMA,
                ),
                timeout=12.0
            )
            payload = _normalize_payload(result, knowledge_context, language)
            local_threshold = _float_env("OLLAMA_ANSWER_CONFIDENCE", 0.68)
            if bool(result.get("answerable")) and payload["confidence"] >= local_threshold:
                payload["routing"] = _route_metadata(
                    cache_hit=False,
                    tier="local",
                    provider="ollama",
                    model=local.model,
                    fallback_reason=None,
                    context_fingerprint=fingerprint,
                )
                remember_answer(
                    question,
                    fingerprint,
                    language,
                    payload,
                    provider="ollama",
                    model=local.model,
                )
                return payload, f"llm:ollama/{local.model}"
            reason = str(result.get("fallback_reason") or "local answer confidence was too low")
            fallback_reasons.append(f"ollama_not_answerable:{reason[:160]}")
        except Exception as exc:
            fallback_reasons.append(f"ollama_unavailable:{type(exc).__name__}")
            print(f"[qa] local Ollama answer failed, trying online fallback: {exc}")
    else:
        fallback_reasons.append("ollama_disabled")

    if llm_available():
        try:
            result = await asyncio.wait_for(
                complete_json(
                    system=(
                        "You answer production-incident and platform questions using only the "
                        "provided data. If the data does not contain the answer, say so explicitly."
                    ),
                    prompt=prompt,
                    schema=ASSISTANT_SCHEMA,
                    schema_name="incident_answer",
                ),
                timeout=25.0
            )
            payload = _normalize_payload(result, knowledge_context, language)
            if payload.get("answer"):
                provider = get_provider() or "online"
                model = get_model()
                payload["routing"] = _route_metadata(
                    cache_hit=False,
                    tier="online",
                    provider=provider,
                    model=model,
                    fallback_reason=";".join(fallback_reasons) or None,
                    context_fingerprint=fingerprint,
                )
                remember_answer(
                    question,
                    fingerprint,
                    language,
                    payload,
                    provider=provider,
                    model=model,
                )
                return payload, f"llm:{provider}/{model}"
        except Exception as exc:
            fallback_reasons.append(f"online_provider_failed:{type(exc).__name__}")
            print(f"[qa] online LLM answer failed, using heuristic: {exc}")
    else:
        fallback_reasons.append("online_provider_not_configured")

    payload = _heuristic_answer(
        record,
        question,
        knowledge_context,
        language,
        system_context=system_context,
        graph_context=graph_context,
    )
    payload["routing"] = _route_metadata(
        cache_hit=False,
        tier="heuristic",
        provider="built-in",
        model="heuristic",
        fallback_reason=";".join(fallback_reasons) or None,
        context_fingerprint=fingerprint,
    )
    return payload, "heuristic"


async def execute_graph_rag(question: str) -> str:
    """Translate user question into a dynamic Cypher query, run on Graph DB,
    and return structured context. Fallbacks to in-memory graph lookup."""
    from agents.llm import complete_json, llm_available
    from agents.query_memory import _run_read, _driver, _MEMORY_INCIDENTS, _MEMORY_EVIDENCE

    if not llm_available():
        return _fallback_graph_rag(question)

    schema_desc = """
    Nodes:
    - Incident (incident_id, service, severity, alert_description, current_status, root_cause, resolution, impact_per_minute, affected_users, owner_team)
    - Service (name, owner_primary, owner_team)
    - Owner (primary, team)
    - Cause (label)
    - Resolution (label)
    - Evidence (title, kind, source_path, content, tags)
    - AgentInvocation (span_id, agent, action, source, timestamp, iteration, reasoning, findings_json, parent_span_id)

    Relationships:
    - (incident)-[:AFFECTS]->(service)
    - (service)-[:OWNED_BY]->(owner)
    - (incident)-[:CAUSED_BY]->(cause)
    - (incident)-[:RESOLVED_BY]->(resolution)
    - (incident)-[:RELATED_TO]->(other_incident)
    - (incident)-[:GROUNDED_BY]->(evidence)
    - (service)-[:DEPENDS_ON]->(other_service)
    - (incident)-[:HAS_INVOCATION]->(AgentInvocation)
    - (AgentInvocation)-[:HANDED_OFF_TO]->(AgentInvocation)
    """

    system_prompt = f"You are a Graph Database assistant. Translate the user's question about production incidents, services, and resolutions into a clean read-only Cypher query. Use this schema:\n{schema_desc}"
    user_prompt = f"Question: {question}\nGenerate a Cypher query to retrieve the answer. Return ONLY a JSON object matching the requested schema."

    schema = {
        "type": "object",
        "properties": {
            "cypher": {"type": "string"}
        },
        "required": ["cypher"],
        "additionalProperties": False,
    }

    cypher_query = ""
    try:
        import asyncio
        res = await asyncio.wait_for(
            complete_json(system=system_prompt, prompt=user_prompt, schema=schema, schema_name="cypher_gen"),
            timeout=4.5
        )
        cypher_query = res.get("cypher", "")
    except Exception as exc:
        print(f"[graph_rag] Cypher generation failed, using in-memory fallback: {exc}")


    if cypher_query and _driver() is not None:
        try:
            print(f"[graph_rag] Executing Cypher: {cypher_query}")
            results = _run_read(cypher_query)
            if results:
                return f"Graph DB Query Results (via Cypher):\n{json.dumps(results[:8], indent=2, ensure_ascii=False)}"
        except Exception as exc:
            print(f"[graph_rag] Cypher execution failed: {exc}")

    return _fallback_graph_rag(question)


def _fallback_graph_rag(question: str) -> str:
    """Locate related graph elements from in-memory graph representation."""
    from agents.query_memory import _MEMORY_INCIDENTS, _MEMORY_EVIDENCE
    terms = [term for term in re.findall(r"[a-zA-Z0-9\-]+", question.lower()) if len(term) > 2]
    if not terms:
        return ""
    matched_data = []

    # Match in-memory incidents and their detailed agent invocations
    for iid, incident in _MEMORY_INCIDENTS.items():
        inv_texts = []
        for inv in incident.get("invocations") or []:
            inv_texts.append(f"{inv.get('agent')} {inv.get('action')} {inv.get('reasoning')} {inv.get('findings') or ''}")
        search_text = f"{incident.get('service')} {incident.get('alert_description')} {incident.get('root_cause')} {incident.get('resolution')} {' '.join(inv_texts)}".lower()
        if any(term in search_text for term in terms):
            matched_data.append({
                "type": "incident",
                "incident_id": iid,
                "service": incident.get("service"),
                "severity": incident.get("severity"),
                "status": incident.get("current_status"),
                "root_cause": incident.get("root_cause"),
                "resolution": incident.get("resolution"),
                "affected_users": incident.get("affected_users"),
                "impact_per_minute": incident.get("impact_per_minute"),
                "agent_invocations": incident.get("invocations")[:3] if incident.get("invocations") else []
            })

    # Match in-memory evidence
    for eid, ev in _MEMORY_EVIDENCE.items():
        search_text = f"{ev.get('title')} {ev.get('content')} {ev.get('source_path')}".lower()
        if any(term in search_text for term in terms):
            matched_data.append({
                "type": "evidence",
                "title": ev.get("title"),
                "kind": ev.get("kind"),
                "source_path": ev.get("source_path"),
                "summary": ev.get("content", "")[:250]
            })

    if matched_data:
        return f"Graph DB Query Results (via In-Memory Fallback):\n{json.dumps(matched_data[:6], indent=2, ensure_ascii=False)}"
    return ""



async def answer_knowledge_question(
    question: str,
    *,
    language_code: str | None = None,
) -> Dict[str, Any]:
    payload, _ = await answer_question({}, question, language_code=language_code)
    return payload


def _float_env(name: str, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _route_metadata(
    *,
    cache_hit: bool,
    tier: str,
    provider: str,
    model: str,
    fallback_reason: str | None,
    context_fingerprint: str,
    cache_match: str | None = None,
    cache_similarity: float | None = None,
) -> Dict[str, Any]:
    route: Dict[str, Any] = {
        "cache_hit": cache_hit,
        "tier": tier,
        "provider": provider,
        "model": model,
        "fallback_reason": fallback_reason,
        "context_fingerprint": context_fingerprint[:16],
    }
    if cache_match:
        route["cache_match"] = cache_match
    if cache_similarity is not None:
        route["cache_similarity"] = cache_similarity
    return route


def _compact(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return str(value)[:500]
    if isinstance(value, str):
        return value[:1600]
    if isinstance(value, list):
        return [_compact(item, depth=depth + 1) for item in value[:8]]
    if isinstance(value, dict):
        return {
            str(key): _compact(item, depth=depth + 1)
            for key, item in list(value.items())[:30]
        }
    return value


def _assistant_prompt(
    record: Dict[str, Any],
    knowledge_context: Dict[str, Any],
    question: str,
    language: str,
    system_context: Dict[str, Any] | None,
    graph_context: str = "",
) -> str:
    ctx = system_context or {}
    sections: list[str] = []

    # --- Platform-wide summary (global queries) ---
    platform = ctx.get("platform_summary") or {}
    if platform:
        sev = json.dumps(platform.get("severity_distribution", {}), ensure_ascii=False)
        top_svcs = ", ".join(
            f"{item['service']}({item['count']})"
            for item in (platform.get("top_affected_services") or [])[:5]
        )
        sections.append(
            f"PLATFORM SNAPSHOT (last 30 days):\n"
            f"  Total incidents: {platform.get('total_incidents_30d') or 0} | "
            f"Active: {platform.get('active_incidents') or 0} | "
            f"Resolved: {platform.get('resolved_incidents') or 0}\n"
            f"  Total users affected: {int(platform.get('total_affected_users_30d') or 0):,}\n"
            f"  Revenue exposure: ${float(platform.get('total_revenue_exposure_per_min_30d') or 0):,.0f}/min\n"
            f"  Severity breakdown: {sev}\n"
            f"  Top services by incident count: {top_svcs}"
        )

    # --- Active incidents ---
    active = ctx.get("active_incidents") or []
    if active:
        lines = []
        for r in active[:4]:
            u = int(r.get("affected_users") or 0)
            imp = float(r.get("revenue_impact_per_min") or 0)
            lines.append(
                f"  [{str(r.get('severity') or '?').upper()}] {r.get('service')} — {r.get('description','')} "
                f"| Users: {u:,} | ${imp:,.0f}/min "
                f"| Status: {r.get('status','?')} | RCA: {r.get('root_cause','pending')}"
            )
        sections.append("ACTIVE INCIDENTS:\n" + "\n".join(lines))

    # --- Query-relevant incidents (pre-fetched by semantic search) ---
    relevant = ctx.get("query_relevant_incidents") or []
    if relevant:
        lines = []
        for r in relevant[:5]:
            conf = f"{float(r.get('rca_confidence') or 0)*100:.0f}%" if r.get("rca_confidence") else "?"
            u = int(r.get("affected_users") or 0)
            imp = float(r.get("revenue_impact_per_min") or 0)
            lines.append(
                f"  [{str(r.get('severity') or '?').upper()}] {r.get('service')} ({r.get('date','?')}) — {r.get('description','')}\n"
                f"    Root Cause: {r.get('root_cause','pending')} (confidence: {conf})\n"
                f"    Resolution: {r.get('resolution_steps','see runbook')}\n"
                f"    Impact: {u:,} users | ${imp:,.0f}/min | Owner: {r.get('owner','?')}"
            )
        sections.append("RELEVANT INCIDENTS FOR THIS QUERY:\n" + "\n".join(lines))

    # --- Recent resolutions ---
    resolved = ctx.get("recently_resolved_incidents") or []
    if resolved:
        lines = []
        for r in resolved[:5]:
            conf = f"{float(r.get('rca_confidence') or 0)*100:.0f}%" if r.get("rca_confidence") else "?"
            u = int(r.get("affected_users") or 0)
            imp = float(r.get("revenue_impact_per_min") or 0)
            date_key = r.get("resolved_date") or r.get("date") or "?"
            lines.append(
                f"  {r.get('service')} ({date_key}) — {r.get('description','')}\n"
                f"    Root Cause: {r.get('root_cause','pending')} (confidence: {conf})\n"
                f"    Resolution: {r.get('resolution_steps','see runbook')}\n"
                f"    Impact: {u:,} users | ${imp:,.0f}/min | Owner: {r.get('owner','?')}"
            )
        sections.append("RECENTLY RESOLVED INCIDENTS:\n" + "\n".join(lines))

    # --- Pre-computed heuristic answer ---
    pre = ctx.get("pre_resolved_answer") or {}
    if pre.get("heuristic_answer"):
        sections.append(
            f"PRE-FETCHED RESOLUTION DATA (use as ground truth):\n"
            f"{str(pre['heuristic_answer'])[:2000]}"
        )

    # --- Service catalog ---
    catalog = ctx.get("service_catalog") or []
    if catalog:
        entries = ", ".join(
            f"{item['service']}(owner:{item.get('owner') or '?'}, {item.get('incident_count',0)} incidents)"
            for item in catalog[:8]
        )
        sections.append(f"SERVICE CATALOG: {entries}")

    # --- Single incident context ---
    incident_ctx = {key: record.get(key) for key in CONTEXT_KEYS if key in record}
    if incident_ctx:
        sections.append(f"INCIDENT CONTEXT:\n{json.dumps(_compact(incident_ctx), ensure_ascii=False, default=str)}")

    # --- Knowledge base ---
    kb_results = (knowledge_context.get("results") or [])[:4]
    if kb_results:
        kb_lines = [f"  - {r.get('title','?')}: {str(r.get('content',''))[:300]}" for r in kb_results]
        sections.append("KNOWLEDGE BASE:\n" + "\n".join(kb_lines))

    if graph_context:
        sections.append(f"GRAPH MEMORY:\n{graph_context[:800]}")

    incidents = [item for item in (ctx.get("incidents") or []) if isinstance(item, dict)]
    if incidents:
        lines = [
            f"  - {item.get('incident_id','?')} {item.get('service','?')} | status={item.get('current_status','?')} | description={str(item.get('alert_description',''))[:120]}"
            for item in incidents[:5]
        ]
        sections.append("SYSTEM INCIDENTS:\n" + "\n".join(lines))

    connectors = [item for item in (ctx.get("connectors") or []) if isinstance(item, dict)]
    if connectors:
        lines = [
            f"  - {item.get('name','?')} | status={item.get('status','?')} | type={item.get('type','?')}"
            for item in connectors[:5]
        ]
        sections.append("SYSTEM CONNECTORS:\n" + "\n".join(lines))

    context_block = "\n\n".join(sections) if sections else "No platform context available."


    return (
        "You are Jarvis, the world-class global agentic incident intelligence assistant for this platform. "
        "You have full visibility into every incident, resolution, service, analytics trend, and platform metric "
        "shown in the context below. You must answer the user's question thoroughly and specifically — "
        "citing actual incident IDs, services, root causes, resolution steps, affected users, revenue impact, "
        "and owner teams from the supplied data. Never say you lack information if it is present in the context. "
        "For 'hi' or greetings, introduce yourself briefly and summarize current platform status. "
        "For analytics questions, quote exact numbers from the data. "
        "For resolution questions, give step-by-step resolution instructions with owner and safety checks. "
        "For pattern questions, identify recurring services or root causes from the data. "
        "Always be specific, data-grounded, and actionable.\n\n"
        f"{context_block}\n\n"
        f"Question: {question}\n"
        f"Requested language code: {language}\n\n"
        "Provide a comprehensive, specific answer using only the data above. "
        "Include relevant numbers, steps, service names, and owners. "
        "Suggest 2-3 concrete follow-up questions the user might want to ask next."
    )



def _normalize_payload(
    result: Dict[str, Any],
    knowledge_context: Dict[str, Any],
    language_code: str,
) -> Dict[str, Any]:
    retrieved = [item for item in (knowledge_context.get("results") or []) if isinstance(item, dict)]
    by_path = {str(item.get("source_path")): item for item in retrieved if item.get("source_path")}
    citations: List[Dict[str, Any]] = []
    for item in result.get("citations") or []:
        if not isinstance(item, dict):
            continue
        source_path = str(item.get("source_path") or "")
        # A model may cite only sources that were actually supplied. Replace its
        # representation with the retrieved record to prevent fabricated paths.
        if source_path in by_path:
            citations.append(by_path[source_path])
    if not citations:
        citations = retrieved[:3]
    follow_ups = result.get("follow_ups") or [
        "What evidence should we check next?",
        "Should we compare this against a prior incident?",
    ]
    answer = str(result.get("answer", "")).strip()
    if not answer:
        answer = "I do not have enough evidence to answer that confidently."
    try:
        confidence = float(result.get("confidence", knowledge_context.get("confidence", 0.0)) or 0.0)
    except (TypeError, ValueError):
        confidence = float(knowledge_context.get("confidence", 0.0) or 0.0)
    return {
        "answer": answer,
        "confidence": round(min(1.0, max(0.0, confidence)), 3),
        "citations": citations,
        "follow_ups": [str(item) for item in follow_ups[:3]],
        "language": str(result.get("language") or language_code or "en"),
        "knowledge": knowledge_context,
    }


def _heuristic_answer(
    record: Dict[str, Any],
    question: str,
    knowledge_context: Dict[str, Any],
    language_code: str,
    *,
    system_context: Dict[str, Any] | None = None,
    graph_context: str = "",
) -> Dict[str, Any]:
    q: str = question.lower()
    rc: Dict[str, Any] = record.get("root_cause") or {}
    retrieved: List[Dict[str, Any]] = list(knowledge_context.get("results", []))
    grounded_kinds = {"incident", "runbook", "postmortem", "similar-incident", "service-profile"}
    incident_citations = [item for item in retrieved if item.get("kind") in grounded_kinds]
    citations: List[Dict[str, Any]] = list(
        (incident_citations if record and incident_citations else retrieved)[:3]
    )
    answer: str

    operational_terms = (
        "root cause", "cause", "why", "impact", "affect", "user", "revenue",
        "status", "deploy", "log", "metric", "fix", "recover", "recommend",
    )
    system_answer = _system_snapshot_answer(question, system_context or {}) if not record else None
    if system_answer:
        answer, system_citations = system_answer
        citations = system_citations
    elif not record and graph_context:
        answer = f"I retrieved the following custom Graph RAG insights to help answer your question:\n\n{graph_context}"
    elif not record and any(term in q for term in operational_terms):
        answer = (
            "No incident was selected or matched, so I cannot determine that value. "
            "Mention the service, alert, or root cause (for example, "
            "'payment-api DB pool incident')."
        )
        citations = []
    elif "similar" in q or "seen" in q or "history" in q:
        sims = record.get("similar_incidents", [])
        if not sims:
            answer = "No similar past incidents found in memory."
        else:
            s = sims[0]
            answer = (
                f"Yes - matches incident #{s.get('number')} on {s.get('service')}: "
                f"{s.get('hypothesis')} ({s.get('match_reason')})."
            )
    elif "deploy" in q or "change" in q or "before" in q:
        answer = rc.get("deploy_correlation") or "No deployment correlation identified."
    elif "root cause" in q or "cause" in q or "why" in q:
        if not rc:
            answer = "Root cause has not been determined yet - agents are still investigating."
        else:
            answer = (
                f"Most likely root cause: {rc.get('hypothesis')} "
                f"({rc.get('confidence', 0) * 100:.0f}% confidence)."
            )
    elif "justify" in q or "justification" in q or "formula" in q or "revenue" in q:
        impact = record.get("revenue_impact_justification") or {}
        if not impact:
            if "affected_users" not in record or record.get("affected_users") is None:
                answer = "Revenue-impact data is not available in this incident record; I cannot calculate it from the retrieved evidence."
            else:
                answer = (
                    f"{record.get('affected_users', 0):,} users affected; estimated revenue "
                    f"impact ${record.get('estimated_revenue_impact_per_minute', 0):.2f}/minute."
                )
        else:
            answer = (
                f"Revenue impact uses {impact.get('formula')}: "
                f"{impact.get('affected_users', 0):,} users x "
                f"${impact.get('revenue_per_user_per_minute', 0):.2f}/user/min = "
                f"${impact.get('revenue_impact_per_minute', 0):.2f}/minute. "
                f"Bounded range is ${impact.get('lower_bound_per_minute', 0):.2f}-"
                f"${impact.get('upper_bound_per_minute', 0):.2f}/minute."
            )
    elif "user" in q or "affect" in q or "impact" in q:
        if "affected_users" not in record or record.get("affected_users") is None:
            answer = "Impact data is not available in this incident record; I cannot determine affected users or revenue impact from the retrieved evidence."
        else:
            answer = (
                f"{record.get('affected_users', 0):,} users affected; estimated revenue "
                f"impact ${record.get('estimated_revenue_impact_per_minute', 0):.2f}/minute."
            )
    elif "log" in q or "error" in q:
        cache = record.get("log_context_cache") or {}
        if "context" in q or "cache" in q or "hierarchy" in q or "history" in q:
            hierarchy = ", ".join(
                f"{h.get('severity')}/{h.get('type')} ({h.get('count')})"
                for h in cache.get("hierarchy", [])
            )
            answer = (
                f"Centralized log cache scanned {cache.get('total_logs_scanned', 0)} logs "
                f"and kept {len(cache.get('error_contexts', []))} error context windows. "
                f"Hierarchy: {hierarchy or 'none'}."
            )
        else:
            types = ", ".join(
                f"{a.get('type')} (x{a.get('count')})" for a in record.get("log_anomalies", [])
            )
            answer = f"Log anomalies detected: {types or 'none'}."
    elif "metric" in q:
        spikes = ", ".join(
            f"{m.get('metric_name')} {m.get('percent_change', 0):+.0f}%"
            for m in record.get("metric_anomalies", [])
        )
        answer = f"Metric anomalies: {spikes or 'none detected'}."
    elif any(term in q for term in ("fix", "recover", "recommend", "next", "resolution", "resolve", "remediate", "solution")):
        recs = record.get("recovery_recommendations", [])
        rollback = record.get("rollback_plan") or {}
        owner = (record.get("ownership") or {}).get("primary") or "service owner"
        guardrails = record.get("kpi_guardrails") or {}
        safety = rollback.get("safety_check") or "; ".join(guardrails.get("operational_guardrails", [])[:2]) or "confirm error rate and latency recover before closing"
        if recs:
            answer = (
                "Resolution plan: "
                + " ".join(f"({i + 1}) {r}" for i, r in enumerate(recs[:4]))
                + f" Human approval required before execution; owner: {owner}; safety check: {safety}."
            )
        elif rc:
            strategy = rollback.get("strategy") or "apply the service runbook remediation and validate health checks"
            answer = (
                f"Resolution plan for {record.get('service', 'this service')}: {strategy}. "
                f"Root cause is {rc.get('hypothesis', 'under investigation')} "
                f"({rc.get('confidence', 0) * 100:.0f}% confidence). "
                f"Escalate to {owner} and validate: {safety}."
            )
        else:
            answer = (
                "Resolution is not ready yet because RCA is still pending. "
                "Start with triage evidence, then execute the service runbook only after human approval."
            )
    elif "owner" in q or "escalat" in q:
        ownership = record.get("ownership") or {}
        path = record.get("escalation_path") or []
        answer = f"Owner: {ownership.get('team', 'unknown')} / {ownership.get('primary', 'unknown')}. Escalation path: {' -> '.join(path) if path else 'not configured'}."
    elif "dependency" in q or "blast" in q or "topology" in q:
        blast = record.get("blast_radius") or {}
        deps = record.get("dependencies") or []
        upstream = record.get("upstream_services") or []
        answer = f"Blast radius is {blast.get('estimated_scope', 'unknown')}: upstream={', '.join(upstream) or 'none'}, dependencies={', '.join(deps) or 'none'}."
    elif "status" in q:
        status = record.get("current_status")
        answer = f"Current status: {status}." if status else "No incident was selected, so its current status cannot be determined."
    else:
        if not record:
            top_result = (knowledge_context.get("results") or [{}])[0]
            excerpt = str(top_result.get("content", "")).strip()
            answer = (
                f"From the retrieved project knowledge: {excerpt}"
                if excerpt
                else "I could not find relevant project knowledge for that question."
            )
        else:
            answer = (
                f"Status: {record.get('current_status', 'unknown')}. "
                f"Root cause: {rc.get('hypothesis', 'not yet determined')}. "
                "Try asking about: root cause, impact, deployments, logs, metrics, "
                "similar incidents, or recommended fixes."
            )

    follow_ups = [
        "Would you like the evidence trail behind this answer?",
        "Should I summarize the impact or recommended next step?",
    ]
    return {
        "answer": answer,
        "confidence": round(max(0.35, float(knowledge_context.get("confidence", 0.0) or 0.0)), 3),
        "citations": citations,
        "follow_ups": follow_ups,
        "language": language_code,
        "knowledge": knowledge_context,
    }


def _system_snapshot_answer(
    question: str,
    system_context: Dict[str, Any],
) -> tuple[str, List[Dict[str, Any]]] | None:
    if not system_context:
        return None
    q = question.casefold()
    incidents = [item for item in (system_context.get("incidents") or []) if isinstance(item, dict)]
    connectors = [item for item in (system_context.get("connectors") or []) if isinstance(item, dict)]
    analytics = system_context.get("analytics") or {}
    system_terms = (
        "system", "platform", "jarvis", "connector", "incident", "weekly",
        "daily", "monthly", "recurr", "overall", "background",
    )
    if not any(term in q for term in system_terms):
        return None
    citations = [
        {
            "title": f"Incident: {item.get('service', 'unknown')}",
            "source_path": f"incident://{item.get('incident_id')}",
            "kind": "incident",
            "content": str(item.get("alert_description") or item.get("root_cause") or ""),
            "score": 0.9,
            "citation": f"incident://{item.get('incident_id')}",
        }
        for item in incidents[:3]
        if item.get("incident_id")
    ]
    if "connector" in q:
        online = sum(1 for item in connectors if item.get("status") in {"online", "configured"})
        return (
            f"The platform has {len(connectors)} configured connectors; {online} are online or configured. "
            "Use the Admin heartbeat to probe any pending or error connector.",
            [],
        )
    total = analytics.get("total", len(incidents))
    recurring = analytics.get("recurring") or []
    active = sum(1 for item in incidents if item.get("current_status") not in {"complete", "resolved", "closed"})
    recurrence = (
        f" The leading recurring signature is {recurring[0].get('signature')} "
        f"({recurring[0].get('count')} occurrences)."
        if recurring
        else " No recurring incident signature is currently identified."
    )
    return (
        f"The current platform snapshot contains {total} incidents in the selected window and "
        f"{active} active incidents.{recurrence}",
        citations,
    )
