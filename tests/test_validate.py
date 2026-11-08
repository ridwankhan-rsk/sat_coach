import pytest

from sat_coach.gen.validate import (
    UnsafeExpression, answers_match, check_math, check_structure, grade, run_check, to_number,
)


def test_run_check_equations_and_expression():
    assert run_check({"equations": ["3*x + 5 = 20"], "solve_for": "x"}) == [5.0]
    assert sorted(run_check({"equations": ["x^2 - 2x - 24 = 0"], "solve_for": "x"})) == [-4.0, 6.0]
    assert run_check({"expression": "0.15*80"}) == [pytest.approx(12.0)]
    sols = run_check({"equations": ["x + y = 14", "x - y = 4"], "solve_for": "x"})
    assert sols == [9.0]


@pytest.mark.parametrize("bad", [
    "__import__('os').system('echo hi')",
    "x.__class__",
    "open('f')",
    "eval(1)",
    "lambda: 1",
])
def test_run_check_rejects_unsafe(bad):
    with pytest.raises(UnsafeExpression):
        run_check({"expression": bad})


def _mcq(answer="B", choices=("3.5", "7", "14", "28"), check=None):
    return {"qtype": "mcq", "stem": "If 4x - 7 = 21, what is x?", "choices": list(choices), "answer": answer,
            "rationale": {L: "..." for L in "ABCD"}, "check": check}


def test_check_math_catches_wrong_key():
    chk = {"equations": ["4*x - 7 = 21"], "solve_for": "x"}
    assert check_math(_mcq("B", check=chk)) == []
    assert check_math(_mcq("C", check=chk))  # 14 is not the solution


def test_structure_errors():
    assert check_structure(_mcq()) == []
    assert "duplicate choices" in check_structure(_mcq(choices=("1", "1", "2", "3")))
    bad = _mcq()
    bad["rationale"] = {"A": "x"}
    assert "rationale needed for every choice" in check_structure(bad)


def test_grading_spr_accepts_equivalent_forms():
    assert grade("spr", "0.6|3/5", "3/5")
    assert grade("spr", "0.6|3/5", ".6")
    assert grade("spr", "16", "16.0")
    assert not grade("spr", "16", "15")
    assert grade("mcq", "B", "b")
    assert not grade("mcq", "B", "")


def test_to_number():
    assert to_number("$1,200") == 1200
    assert to_number("7/2") == 3.5
    assert to_number("x+1") is None
    assert answers_match({"qtype": "mcq", "answer": "C"}, "C) 12")
