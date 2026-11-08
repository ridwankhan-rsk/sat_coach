"""Generate → validate → store. Every item passes schema checks, a sympy check of the keyed
answer (math, when computable), and a blind re-solve by a separate model call before it is
saved. Calls are batched (one writer call + one solver call per batch) to stay well inside
free-tier rate limits."""

from __future__ import annotations

import logging
from typing import Any

from ..db import Passage, Question, session
from ..llm import LLM, LLMError
from ..rag.index import retrieve_exemplars
from ..taxonomy import SKILL_BY_ID
from . import prompts
from .validate import answers_match, check_math, check_structure

log = logging.getLogger(__name__)


def _solve_batch(llm: LLM, items: list[dict[str, Any]]) -> list[str]:
    blocks = []
    for i, it in enumerate(items, 1):
        blocks.append(f"### Question {i}\n" + prompts.solve_prompt(it["stem"], it.get("choices") or [], it.get("qtype", "mcq"), it.get("excerpt")))
    prompt = (
        "Solve each question independently.\n\n" + "\n\n".join(blocks)
        + '\n\nReturn {"answers": ["<answer to Q1>", "<answer to Q2>", ...]} (letters for multiple choice, numbers otherwise).'
    )
    out = llm.generate_json(prompts.SYSTEM_SOLVER, prompt, check=True)
    answers = out.get("answers", []) if isinstance(out, dict) else []
    return [str(a) for a in answers] + [""] * (len(items) - len(answers))


def _normalize(it: dict[str, Any]) -> dict[str, Any]:
    it = dict(it)
    it["qtype"] = "spr" if str(it.get("qtype", "mcq")).lower() == "spr" else "mcq"
    if it["qtype"] == "spr":
        it["choices"] = []
    it["answer"] = str(it.get("answer", "")).strip()
    if it["qtype"] == "mcq":
        it["answer"] = it["answer"].upper()[:1]
    it["choices"] = [str(c) for c in (it.get("choices") or [])]
    return it


def validate_batch(llm: LLM, items: list[dict[str, Any]], section: str) -> list[tuple[dict[str, Any], list[str]]]:
    """Return (item, problems) pairs; an empty problem list means the item is accepted."""
    items = [_normalize(it) for it in items]
    results = []
    for it in items:
        errs = check_structure(it)
        if section == "math" and not errs:
            errs += check_math(it)
        results.append((it, errs))
    survivors = [it for it, e in results if not e]
    if survivors:
        solved = _solve_batch(llm, survivors)
        by_id = {id(it): ans for it, ans in zip(survivors, solved)}
        for it, errs in results:
            if not errs and not answers_match(it, by_id[id(it)]):
                errs.append(f"blind re-solve answered {by_id[id(it)]!r}, key is {it['answer']!r}")
    return results


def generate_math(llm: LLM, slots: list[tuple[str, str]], variant_of: Question | None = None,
                  rounds: int = 2) -> list[Question | None]:
    """Generate one math question per (skill, difficulty) slot. Returns saved Questions in
    slot order, with None for slots that never produced a valid item."""
    out: list[Question | None] = [None] * len(slots)
    pending = list(range(len(slots)))
    for _ in range(rounds):
        if not pending:
            break
        req = [slots[i] for i in pending]
        exemplars = {}
        for skill, diff in req:
            query = variant_of.stem if variant_of is not None else SKILL_BY_ID[skill].name
            exemplars[skill] = retrieve_exemplars("math", skill, diff, query=query, k=3)
        try:
            data = llm.generate_json(prompts.SYSTEM_WRITER, prompts.math_prompt(req, exemplars, variant_of))
            items = data.get("items", []) if isinstance(data, dict) else []
            checked = validate_batch(llm, items[: len(req)], "math")
        except LLMError as e:
            log.warning("math generation failed: %s", e)
            continue
        still = []
        for idx, slot_i in enumerate(pending):
            if idx < len(checked) and not checked[idx][1]:
                skill, diff = slots[slot_i]
                out[slot_i] = _save(checked[idx][0], "math", skill, diff, parent=variant_of)
            else:
                if idx < len(checked):
                    log.info("rejected math item for %s: %s", slots[slot_i], checked[idx][1])
                still.append(slot_i)
        pending = still
    return out


def generate_rw_set(llm: LLM, passage: Passage, skills: list[str], difficulties: list[str]) -> list[Question]:
    """Write a question set (one per skill) about a real passage. Returns accepted items."""
    exemplars = {sk: retrieve_exemplars("rw", sk, None, k=2) for sk in skills}
    accepted: list[Question] = []
    remaining = list(zip(skills, difficulties))
    for _ in range(2):
        if not remaining:
            break
        sk_list, diff_list = [r[0] for r in remaining], [r[1] for r in remaining]
        try:
            data = llm.generate_json(
                prompts.SYSTEM_WRITER,
                prompts.rw_set_prompt(passage.title, passage.author, passage.text, sk_list, exemplars, diff_list),
            )
            items = data.get("items", []) if isinstance(data, dict) else []
            checked = validate_batch(llm, items[: len(remaining)], "rw")
        except LLMError as e:
            log.warning("rw generation failed: %s", e)
            continue
        still = []
        for idx, (skill, diff) in enumerate(remaining):
            if idx < len(checked) and not checked[idx][1]:
                accepted.append(_save(checked[idx][0], "rw", skill, diff, passage_id=passage.id))
            else:
                still.append((skill, diff))
        remaining = still
    return accepted


def _save(it: dict[str, Any], section: str, skill: str, difficulty: str, parent: Question | None = None,
          passage_id: int | None = None) -> Question:
    sk = SKILL_BY_ID[skill]
    q = Question(
        section=section, domain=sk.domain, skill=skill, difficulty=difficulty, qtype=it["qtype"],
        stem=it["stem"].strip(), choices=it["choices"], answer=it["answer"], rationale=it.get("rationale") or {},
        excerpt=(it.get("excerpt") or None), passage_id=passage_id if passage_id is not None else (parent.passage_id if parent else None),
        origin="generated", parent_id=parent.id if parent else None,
    )
    with session() as s:
        s.add(q)
        s.commit()
        s.refresh(q)
    return q
