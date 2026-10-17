"""Input-history evidence only; fake outputs do not establish live model quality."""
from __future__ import annotations

import asyncio
import logging

from app.interviewer import SUMMARY_INSTRUCTION, AdkInterviewer
from app.states import InterviewAnswer, TraitSummary


def test_summary_receives_all_six_specific_answer_pairs(monkeypatch, caplog):
    pairs = [
        ("How do you start the day?", "I line up my shoes before a sunrise walk."),
        ("How do you socialise?", "I host two friends for quiet board games."),
        ("How do you handle stress?", "I write a checklist before choosing one task."),
        ("What absorbs your attention?", "I repair old radios on weekends."),
        ("What is an unusual habit?", "I sort tea tins by the weather they suit."),
        ("What motivates you?", "I like making broken things useful for neighbours."),
    ]
    history = [InterviewAnswer(question=q, answer=a) for q, a in pairs]
    captured = []
    expected = TraitSummary(summary="Synthetic contract fixture, not a live quality result.",
                            traits=["methodical"])

    async def fake_run(self, **kwargs):
        captured.append(kwargs)
        return expected

    monkeypatch.setattr(AdkInterviewer, "_run_adk", fake_run)
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(AdkInterviewer("synthetic-key").summarise(history))
    assert result is expected
    assert len(captured) == 1
    request = captured[0]
    assert request["output_schema"] is TraitSummary
    assert request["instruction"] == SUMMARY_INSTRUCTION
    assert "Do not invent facts" in request["instruction"]
    prompt = request["prompt"]
    positions = []
    for q, a in pairs:
        pair = f"Q: {q}\nA: {a}"
        assert prompt.count(pair) == 1
        positions.append(prompt.index(pair))
        assert a not in caplog.text
    assert positions == sorted(positions)
    assert "synthetic-key" not in caplog.text
    assert [(p.question, p.answer) for p in history] == pairs
