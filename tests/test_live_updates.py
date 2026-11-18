"""Tests for the cross-tab live-update broadcast in app/ui.py."""
from app import ui as page
from app.api_client import Part

PART = Part(uuid="p1", part_no="TM-1020", name="Power Cell", status="recalled", unit="pcs")


def test_broadcast_reaches_every_tab_except_the_sender(monkeypatch):
    monkeypatch.setattr(page, "_part_listeners", set())
    received = {"a": [], "b": [], "c": []}
    tab_a, tab_b, tab_c = (lambda p, k=k: received[k].append(p) for k in "abc")
    page._part_listeners.update({tab_a, tab_b, tab_c})

    page.broadcast_part_change(PART, source=tab_a)

    assert received == {"a": [], "b": [PART], "c": [PART]}


def test_a_tab_closing_during_broadcast_is_safe(monkeypatch):
    monkeypatch.setattr(page, "_part_listeners", set())
    calls = []

    def closing_tab(part):
        page._part_listeners.discard(closing_tab)  # e.g. the tab is deleted mid-loop
        calls.append("closing")

    def other_tab(part):
        calls.append("other")

    page._part_listeners.update({closing_tab, other_tab})
    page.broadcast_part_change(PART)

    assert sorted(calls) == ["closing", "other"]
