"""Prompt templates. Rationales follow the wording of College Board's answer explanations:
"Choice B is the best answer because ..." / "Choice A is incorrect because ..."."""

from __future__ import annotations

import json

from ..db import Question
from ..taxonomy import SKILL_BY_ID, domain_name

SYSTEM_WRITER = """You write original practice questions for the Digital SAT (the College Board's \
digital SAT Suite). Match the official test's style exactly: concise stems, four answer choices \
(A-D) with exactly one defensible answer, plausible distractors that reflect real student errors, \
and difficulty calibrated to the requested level. Never copy an exemplar; write a new question \
that tests the same skill.

Write explanations in the voice of the official SAT answer explanations:
- The correct choice: "Choice X is the best answer because ..." then the reasoning or the work.
- Each wrong choice: "Choice Y is incorrect because ..." and name the likely error \
(e.g. "This is the value of 2x, not x." or "The passage doesn't suggest that ...").
Use plain text. For math, write expressions in readable plain text or LaTeX between $...$."""

SYSTEM_SOLVER = """You are an expert Digital SAT test taker. Solve the question carefully and \
return only your final answer as JSON."""

MATH_ITEM_SHAPE = """{
  "qtype": "mcq" or "spr",
  "stem": "question text",
  "choices": ["...", "...", "...", "..."],      // mcq only; omit or [] for spr
  "answer": "B",                                 // mcq: letter. spr: the value, e.g. "12" or "7/2"
  "rationale": {"A": "...", "B": "...", "C": "...", "D": "..."},  // spr: {"correct": "..."}
  "check": {"equations": ["3*x + 5 = 20"], "solve_for": "x"}      // or {"expression": "0.15*80"}; null if not computable
}"""


def _render_exemplar(q: Question) -> str:
    d = {"qtype": q.qtype, "difficulty": q.difficulty, "stem": q.stem, "answer": q.answer}
    if q.qtype == "mcq":
        d["choices"] = q.choices
    if q.excerpt:
        d["text"] = q.excerpt
    return json.dumps(d, ensure_ascii=False)


def math_prompt(slots: list[tuple[str, str]], exemplars: dict[str, list[Question]], variant_of: Question | None = None) -> str:
    lines = [f"Write {len(slots)} new Digital SAT math question(s), one per request below.", ""]
    for i, (skill, diff) in enumerate(slots, 1):
        sk = SKILL_BY_ID[skill]
        lines.append(f"Request {i}: domain = {domain_name(sk.domain)}; skill = {sk.name}; difficulty = {diff}.")
        ex = exemplars.get(skill) or []
        if ex:
            lines.append("  Real examples of this skill (style reference only, do not copy):")
            lines += [f"  - {_render_exemplar(q)}" for q in ex]
    if variant_of is not None:
        lines += [
            "",
            "The student previously missed this question. Write a NEW question that tests the same idea "
            "and the same trap, with different numbers and context:",
            _render_exemplar(variant_of),
        ]
    lines += [
        "",
        "About 1 in 4 questions may be student-produced response (spr) as on the real test; the rest mcq.",
        "Include a `check` the grader can run with sympy whenever the answer is a number: either an "
        "expression that evaluates to the answer or equations plus the variable/expression to solve for. "
        "Use only numbers, + - * / ^, parentheses, single-letter variables, sqrt, sin, cos, tan, pi.",
        "",
        'Return JSON: {"items": [ITEM, ...]} in request order, where ITEM is:',
        MATH_ITEM_SHAPE,
    ]
    return "\n".join(lines)


def rw_set_prompt(passage_title: str, passage_author: str, passage_text: str, skills: list[str],
                  exemplars: dict[str, list[Question]], difficulties: list[str]) -> str:
    lines = [
        "Below is a real published text. Write Digital SAT Reading and Writing questions about it, "
        "one per request. Each question must be answerable from the text alone.",
        "",
        f'TEXT: "{passage_title}" by {passage_author}',
        "<<<",
        passage_text,
        ">>>",
        "",
        "For each question, set `excerpt` to the 25-150 word portion of the text the question is about, "
        "quoted exactly (the student sees it highlighted). For Transitions, Boundaries, and Form, Structure, "
        "and Sense questions, the excerpt is the quoted sentence(s) with one blank written as ______, and the "
        "choices fill the blank; keep the original wording everywhere else. The keyed answer must follow "
        "current Standard English conventions, even if the original (older) text punctuates differently.",
        "",
    ]
    for i, (skill, diff) in enumerate(zip(skills, difficulties), 1):
        sk = SKILL_BY_ID[skill]
        lines.append(f"Request {i}: {domain_name(sk.domain)} / {sk.name}; difficulty = {diff}.")
        for q in exemplars.get(skill) or []:
            lines.append(f"  example: {_render_exemplar(q)}")
    lines += [
        "",
        'Return JSON: {"items": [{"skill": "...", "excerpt": "...", "stem": "...", "choices": [4 strings], '
        '"answer": "A-D", "rationale": {"A": "...", "B": "...", "C": "...", "D": "..."}}, ...]}',
    ]
    return "\n".join(lines)


def solve_prompt(stem: str, choices: list[str], qtype: str, excerpt: str | None = None) -> str:
    parts = []
    if excerpt:
        parts += ["Text:", excerpt, ""]
    parts.append(stem)
    if qtype == "mcq":
        parts += [f"{L}) {c}" for L, c in zip("ABCD", choices)]
        parts.append('\nReturn {"answer": "<letter A-D>"}.')
    else:
        parts.append('\nThis is a student-produced response question. Return {"answer": "<number>"}.')
    return "\n".join(parts)
