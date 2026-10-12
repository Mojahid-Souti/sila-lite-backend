"""SILA LangGraph orchestrator (WS-8).

A StateGraph runtime for the concierge: durable state on a Postgres checkpointer
(thread = user+session) and real interrupt()/resume. Built behind the
SILA_ORCHESTRATOR flag so the flat `converse` loop stays the default until the
graph is proven.
"""
