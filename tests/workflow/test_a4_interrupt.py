"""Assumption A4: on resume LangGraph re-runs the interrupted node from its START (code before interrupt() runs again)."""
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class S(TypedDict):
    x: int


def test_interrupt_node_reruns_from_start_but_prior_nodes_do_not():
    calls: list[str] = []

    def a(s):
        calls.append("a")
        return {"x": 1}

    def b(s):
        calls.append("b-before")
        v = interrupt("q")
        calls.append("b-after")
        return {"x": v}

    g = StateGraph(S)
    g.add_node("a", a)
    g.add_node("b", b)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    app = g.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t"}}
    app.invoke({"x": 0}, cfg)
    assert calls == ["a", "b-before"]
    out = app.invoke(Command(resume=5), cfg)
    assert out["x"] == 5
    assert calls == ["a", "b-before", "b-before", "b-after"]  # b re-ran from its start; a did not
