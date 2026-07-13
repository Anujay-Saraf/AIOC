Agent Handoff vs LangGraph State Machine
=======================================

Overview
--------
This document explains the separation between the lightweight Router / Agent-hand-off flow
and the LangGraph execution state machine used by this repository.

Why two layers?
----------------
- Router / Agent Handoff (`agents/router_agent.py`)
  - Purpose: business and policy decisions — what should run next
  - Characteristics: small, deterministic, easy to unit-test, contains safety gates and human-in-the-loop checks
  - Examples: decide between `analyze_logs`, `request_more_data`, `incident_commander` based on RCA confidence

- LangGraph Execution Engine (`langgraph` / compiled graphs)
  - Purpose: runtime orchestration and execution of agent nodes
  - Characteristics: handles node-level concurrency, retries, callbacks, streaming updates, and complex node graphs
  - Examples: running the RCA agent, invoking evidence collectors, posting updates, invoking remediation steps

Recommended Pattern
-------------------
- Keep policy decisions in the Router. The Router answers "what" at a coarse granularity.
- Implement the detailed "how" for each action as a LangGraph subgraph. LangGraph handles the orchestration and node lifecycle.
- Let the Router trigger LangGraph by selecting the next action; LangGraph then executes the chosen subgraph and emits node updates back to the Router / app.

Observability and Testing
-------------------------
- Unit test `agents/router_agent.py` for decision logic.
- Use LangGraph integration tests to validate node-level behaviors.
- The app's `_run_analysis` callback persists intermediate state to `incident_store` so both layers remain observable in the UI.

When to collapse layers
-----------------------
Collapse only if the system must support extremely dynamic policy changes that cannot be represented as a routing decision. Most teams benefit from keeping the separation for clarity and maintainability.


If you want, I can add inline documentation comments in `agents/router_agent.py` and `agents/agentic_system.py` to make this explicit in-code.