"""
state_graph.py

Compiles the LangGraph DAG. All node LOGIC lives in graph/nodes.py — this
file only imports those real implementations and wires them into edges.
It intentionally defines no node bodies of its own; if you're looking for
what diagnose/governor/execute actually DO, that's in nodes.py, not here.

    classify --(iteration_guard)--> diagnose --> governor --(governor_routing)--> execute --> END
                    |                                              |
                    +--> END (halt)                                +--> END (blocked)

build_graph(policy) takes a MerchantPolicy so Tier 2 rules can be swapped
per-merchant without touching graph structure.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_langgraph = import_module("langgraph.graph")
StateGraph: Any = _langgraph.StateGraph
START: Any = _langgraph.START
END: Any = _langgraph.END

from ..core_state import RecoveryState
from ..config import MerchantPolicy, DEFAULT_POLICY
from . import nodes


def build_graph(policy: MerchantPolicy = DEFAULT_POLICY):
    graph = StateGraph(RecoveryState)

    graph.add_node("classify", nodes.classify_node)
    graph.add_node("diagnose", nodes.diagnose_node)
    graph.add_node("governor", getattr(nodes, "_make_governor_node")(policy))
    graph.add_node("execute", nodes.execute_node)

    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify", nodes.iteration_guard,
        {"continue": "diagnose", "halt": END},
    )
    graph.add_edge("diagnose", "governor")
    graph.add_conditional_edges(
        "governor", nodes.governor_routing,
        {"execute": "execute", "end": END},
    )
    graph.add_edge("execute", END)

    return graph.compile()


# Default compiled instance for the common case (default merchant policy).
# A caller needing a different policy should call build_graph(custom_policy)
# directly rather than mutate this one.
default_graph = build_graph(DEFAULT_POLICY)