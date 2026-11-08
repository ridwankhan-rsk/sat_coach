"""Digital SAT content taxonomy, following College Board's published test specifications.

Domain weights are the approximate share of each section's questions.
"""

from __future__ import annotations

from dataclasses import dataclass

DIFFICULTIES = ("easy", "medium", "hard")
# Item difficulty on the ability (theta) scale used by coach.mastery.
DIFFICULTY_B = {"easy": -1.0, "medium": 0.0, "hard": 1.0}


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    domain: str
    section: str  # "math" | "rw"
    article_ok: bool = True  # R&W only: can be asked about an excerpt of a longer article


MATH_DOMAINS = {
    "algebra": ("Algebra", 0.35),
    "advanced_math": ("Advanced Math", 0.35),
    "psda": ("Problem-Solving and Data Analysis", 0.15),
    "geometry_trig": ("Geometry and Trigonometry", 0.15),
}

RW_DOMAINS = {
    "information_ideas": ("Information and Ideas", 0.26),
    "craft_structure": ("Craft and Structure", 0.28),
    "expression_ideas": ("Expression of Ideas", 0.20),
    "conventions": ("Standard English Conventions", 0.26),
}

_M = "math"
_R = "rw"

SKILLS: list[Skill] = [
    # --- Math: Algebra
    Skill("linear_eq_one_var", "Linear equations in one variable", "algebra", _M),
    Skill("linear_functions", "Linear functions", "algebra", _M),
    Skill("linear_eq_two_var", "Linear equations in two variables", "algebra", _M),
    Skill("systems_linear", "Systems of two linear equations in two variables", "algebra", _M),
    Skill("linear_inequalities", "Linear inequalities in one or two variables", "algebra", _M),
    # --- Math: Advanced Math
    Skill("nonlinear_functions", "Nonlinear functions", "advanced_math", _M),
    Skill("nonlinear_equations", "Nonlinear equations in one variable and systems in two variables", "advanced_math", _M),
    Skill("equivalent_expressions", "Equivalent expressions", "advanced_math", _M),
    # --- Math: Problem-Solving and Data Analysis
    Skill("ratios_rates", "Ratios, rates, proportional relationships, and units", "psda", _M),
    Skill("percentages", "Percentages", "psda", _M),
    Skill("one_var_data", "One-variable data: distributions and measures of center and spread", "psda", _M),
    Skill("two_var_data", "Two-variable data: models and scatterplots", "psda", _M),
    Skill("probability", "Probability and conditional probability", "psda", _M),
    Skill("inference_stats", "Inference from sample statistics and margin of error", "psda", _M),
    Skill("evaluating_claims", "Evaluating statistical claims: observational studies and experiments", "psda", _M),
    # --- Math: Geometry and Trigonometry
    Skill("area_volume", "Area and volume", "geometry_trig", _M),
    Skill("lines_angles_triangles", "Lines, angles, and triangles", "geometry_trig", _M),
    Skill("right_triangles_trig", "Right triangles and trigonometry", "geometry_trig", _M),
    Skill("circles", "Circles", "geometry_trig", _M),
    # --- R&W: Information and Ideas
    Skill("central_ideas", "Central Ideas and Details", "information_ideas", _R),
    Skill("evidence_textual", "Command of Evidence (Textual)", "information_ideas", _R),
    Skill("evidence_quantitative", "Command of Evidence (Quantitative)", "information_ideas", _R, article_ok=False),
    Skill("inferences", "Inferences", "information_ideas", _R),
    # --- R&W: Craft and Structure
    Skill("words_in_context", "Words in Context", "craft_structure", _R),
    Skill("text_structure_purpose", "Text Structure and Purpose", "craft_structure", _R),
    Skill("cross_text", "Cross-Text Connections", "craft_structure", _R, article_ok=False),
    # --- R&W: Expression of Ideas
    Skill("rhetorical_synthesis", "Rhetorical Synthesis", "expression_ideas", _R, article_ok=False),
    Skill("transitions", "Transitions", "expression_ideas", _R),
    # --- R&W: Standard English Conventions
    Skill("boundaries", "Boundaries", "conventions", _R),
    Skill("form_structure_sense", "Form, Structure, and Sense", "conventions", _R),
]

SKILL_BY_ID: dict[str, Skill] = {s.id: s for s in SKILLS}


def domains(section: str) -> dict[str, tuple[str, float]]:
    return MATH_DOMAINS if section == "math" else RW_DOMAINS


def skills_in(section: str, domain: str | None = None, article_only: bool = False) -> list[Skill]:
    out = [s for s in SKILLS if s.section == section and (domain is None or s.domain == domain)]
    if article_only:
        out = [s for s in out if s.article_ok]
    return out


def domain_name(domain: str) -> str:
    return {**MATH_DOMAINS, **RW_DOMAINS}[domain][0]
