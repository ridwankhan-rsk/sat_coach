"""Checks applied to every generated question before it reaches a student."""

from __future__ import annotations

import re
from typing import Any

import sympy as sp
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

LETTERS = "ABCD"

# Only these names may appear in a model-written check. Anything else (attribute access,
# dunder names, imports, ...) is rejected before sympy ever sees the string, because
# sympy's parser evaluates its input.
_ALLOWED_FUNCS = {
    "sqrt": sp.sqrt, "sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "asin": sp.asin,
    "acos": sp.acos, "atan": sp.atan, "log": sp.log, "ln": sp.log, "exp": sp.exp,
    "Abs": sp.Abs, "abs": sp.Abs, "pi": sp.pi, "Rational": sp.Rational, "E": sp.E,
}
_SAFE_CHARS = re.compile(r"^[0-9A-Za-z_+\-*/^().,= <>\s]*$")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_TRANSFORMS = standard_transformations + (convert_xor, implicit_multiplication_application)


class UnsafeExpression(ValueError):
    pass


def _parse(expr: str, symbols: dict[str, sp.Symbol]):
    if len(expr) > 300 or not _SAFE_CHARS.match(expr) or "__" in expr:
        raise UnsafeExpression(expr)
    for name in _IDENT.findall(expr):
        if name in _ALLOWED_FUNCS:
            continue
        if len(name) <= 2 and name.isalpha():
            symbols.setdefault(name, sp.Symbol(name, real=True))
            continue
        raise UnsafeExpression(f"name {name!r} not allowed")
    local = {**_ALLOWED_FUNCS, **symbols}
    return parse_expr(expr, local_dict=local, global_dict={"__builtins__": {}, **_sympy_core()}, transformations=_TRANSFORMS)


def _sympy_core() -> dict[str, Any]:
    return {"Integer": sp.Integer, "Float": sp.Float, "Symbol": sp.Symbol, "Rational": sp.Rational,
            "Mul": sp.Mul, "Add": sp.Add, "Pow": sp.Pow}


def to_number(text: str) -> float | None:
    """Parse '12', '-3.5', '7/2', '$1,200', '25%' into a float; None if not a plain number."""
    if text is None:
        return None
    t = str(text).strip().replace(",", "").replace("$", "").replace("\\", "")
    t = t.rstrip("%").strip()  # percents compare as written: "25%" -> 25
    m = re.fullmatch(r"(-?\d*\.?\d+)(?:\s*/\s*(-?\d*\.?\d+))?", t)
    if not m:
        return None
    den = float(m.group(2)) if m.group(2) else 1.0
    return float(m.group(1)) / den if den else None


def run_check(check: dict[str, Any]) -> list[float]:
    """Evaluate a model-written sympy check. Returns the candidate numeric answers.

    Accepted shapes:
      {"expression": "(3/4)*120"}
      {"equations": ["3*x + 5 = 20"], "solve_for": "x"}
    """
    symbols: dict[str, sp.Symbol] = {}
    if "expression" in check and check["expression"]:
        val = _parse(str(check["expression"]), symbols)
        return [float(sp.N(val))]
    eqs_raw = check.get("equations") or []
    target = str(check.get("solve_for", "x"))
    eqs = []
    for e in eqs_raw:
        if "=" in e:
            lhs, rhs = e.split("=", 1)
            eqs.append(sp.Eq(_parse(lhs, symbols), _parse(rhs.lstrip("="), symbols)))
        else:
            eqs.append(_parse(e, symbols))
    if not eqs:
        raise ValueError("empty check")
    target_expr = _parse(target, symbols)
    unknowns = sorted({s for e in eqs for s in e.free_symbols}, key=str)
    sols = sp.solve(eqs, unknowns, dict=True)
    out = []
    for sol in sols:
        v = target_expr.subs(sol)
        if v.free_symbols:
            continue
        v = sp.N(v)
        if v.is_real:
            out.append(float(v))
    return out


def close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b)) + 1e-9


def check_structure(item: dict[str, Any]) -> list[str]:
    """Schema-level problems with a generated item ([] means OK)."""
    errs = []
    if not str(item.get("stem", "")).strip():
        errs.append("missing stem")
    qtype = item.get("qtype", "mcq")
    if qtype == "mcq":
        ch = item.get("choices") or []
        if len(ch) != 4:
            errs.append("mcq needs exactly 4 choices")
        elif len({str(c).strip().lower() for c in ch}) != 4:
            errs.append("duplicate choices")
        if str(item.get("answer", "")).strip().upper() not in LETTERS:
            errs.append("answer must be A-D")
        rat = item.get("rationale") or {}
        if any(not str(rat.get(L, "")).strip() for L in LETTERS):
            errs.append("rationale needed for every choice")
    elif qtype == "spr":
        if not str(item.get("answer", "")).strip():
            errs.append("spr needs an answer")
        if not str((item.get("rationale") or {}).get("correct", "")).strip():
            errs.append("spr needs a rationale")
    else:
        errs.append(f"unknown qtype {qtype}")
    if len(str(item.get("stem", ""))) > 1500:
        errs.append("stem too long")
    return errs


def check_math(item: dict[str, Any]) -> list[str]:
    """Verify the keyed answer against the sympy check, when the item supplies one."""
    check = item.get("check")
    if not check:
        return []
    try:
        values = run_check(check)
    except UnsafeExpression as e:
        return [f"unsafe check expression: {e}"]
    except Exception as e:  # noqa: BLE001 - sympy raises many types
        return [f"check failed to evaluate: {e}"]
    if not values:
        return ["check produced no real solution"]
    keyed = keyed_value(item)
    if keyed is None:
        return []  # non-numeric answer (an expression or statement); rely on the blind re-solve
    if not any(close(keyed, v) for v in values):
        return [f"keyed answer {keyed} does not match computed {values}"]
    return []


def keyed_value(item: dict[str, Any]) -> float | None:
    if item.get("qtype") == "spr":
        return to_number(str(item.get("answer", "")).split("|")[0])
    letter = str(item.get("answer", "")).strip().upper()
    if letter in LETTERS and len(item.get("choices") or []) == 4:
        return to_number(item["choices"][LETTERS.index(letter)])
    return None


def answers_match(item: dict[str, Any], solved: str) -> bool:
    """Does an independent solver's answer agree with the key?"""
    solved = str(solved).strip()
    if item.get("qtype") == "spr":
        accepted = [a.strip() for a in str(item["answer"]).split("|")]
        sv = to_number(solved)
        return any(a == solved or (sv is not None and to_number(a) is not None and close(sv, to_number(a))) for a in accepted)
    return solved.upper()[:1] == str(item["answer"]).strip().upper()


def grade(qtype: str, answer: str, response: str) -> bool:
    if not response:
        return False
    return answers_match({"qtype": qtype, "answer": answer}, response)
