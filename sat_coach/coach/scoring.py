"""Projected score: per-skill ability → per-domain ability → section score on the 200-800 scale.

This is an estimate from practice data, not an official score. The band narrows as the
student answers more questions in every domain.
"""

from __future__ import annotations

import math

from ..db import SkillState
from ..taxonomy import domains, skills_in


def _round10(x: float) -> int:
    return int(round(x / 10.0) * 10)


def domain_ability(states: dict[str, SkillState], section: str) -> dict[str, tuple[float, float]]:
    """(theta, uncertainty) per domain, weighting skills by how much evidence each has."""
    out = {}
    for dom in domains(section):
        sks = [states[s.id] for s in skills_in(section, dom) if s.id in states]
        if not sks:
            out[dom] = (0.0, 1.0)
            continue
        w = [1.0 + st.attempts for st in sks]
        theta = sum(st.theta * wi for st, wi in zip(sks, w)) / sum(w)
        unc = sum(st.unc * wi for st, wi in zip(sks, w)) / sum(w)
        out[dom] = (theta, unc)
    return out


def section_score(states: dict[str, SkillState], section: str) -> tuple[int, int]:
    """(score, half-width of band) for one section."""
    abil = domain_ability(states, section)
    weights = domains(section)
    theta = sum(abil[d][0] * weights[d][1] for d in weights)
    unc = sum(abil[d][1] * weights[d][1] for d in weights)
    score = 200 + 600 / (1 + math.exp(-1.1 * theta))
    return max(200, min(800, _round10(score))), _round10(30 + 120 * unc)


def projected(states: dict[str, SkillState]) -> dict:
    m, mb = section_score(states, "math")
    r, rb = section_score(states, "rw")
    return {
        "math": m, "math_band": mb,
        "rw": r, "rw_band": rb,
        "total": m + r,
        "band": _round10(math.sqrt(mb**2 + rb**2)),
    }
