from __future__ import annotations

import random
from pathlib import Path

import typer
from sqlmodel import func, select

from .db import Passage, Question, User, session

app = typer.Typer(help="SAT Coach: adaptive Digital SAT practice.", no_args_is_help=True)
users_app = typer.Typer(help="Manage local profiles.")
app.add_typer(users_app, name="users")


@app.command()
def init():
    """Create the database and load the seed question bank."""
    from .seed import load_seed

    added = load_seed()
    typer.echo(f"Seed loaded: {added['questions']} new questions, {added['passages']} new passages.")
    _bank()


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000):
    """Start the web app (open http://127.0.0.1:8000)."""
    import uvicorn

    from .seed import load_seed

    load_seed()
    typer.echo(f"SAT Coach running at http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    uvicorn.run("sat_coach.api:app", host=host, port=port)


@app.command("fetch-passages")
def fetch_passages(gutenberg: bool = True, plos: bool = True):
    """Download public-domain (Gutenberg) and CC BY (PLOS) passages for R&W questions."""
    from .ingest.passages import fetch_all

    typer.echo(f"Added {fetch_all(gutenberg=gutenberg, plos=plos)} passages.")


@app.command("import-pdfs")
def import_pdfs(folder: Path = typer.Argument(..., exists=True, file_okay=False)):
    """Import official practice PDFs you downloaded (kept private in data/private/)."""
    from .ingest.pdf_import import import_pdf
    from .llm import get_llm

    llm = get_llm()
    if llm is None:
        raise typer.BadParameter("Set GEMINI_API_KEY (or ANTHROPIC_API_KEY) in .env first.")
    for pdf in sorted(folder.glob("*.pdf")):
        typer.echo(f"{pdf.name}: {import_pdf(llm, pdf)} questions imported")


@app.command()
def generate(math: int = 8, rw_sets: int = 1, seed: int | None = None):
    """Pre-generate questions into the bank, so sessions start instantly and use fewer API calls."""
    from .gen.generate import generate_math, generate_rw_set
    from .llm import get_llm
    from .taxonomy import DIFFICULTIES, RW_DOMAINS, skills_in

    llm = get_llm()
    if llm is None:
        raise typer.BadParameter("Set GEMINI_API_KEY (or ANTHROPIC_API_KEY) in .env first.")
    rng = random.Random(seed)
    slots = [(rng.choice(skills_in("math")).id, rng.choice(DIFFICULTIES)) for _ in range(math)]
    for i in range(0, len(slots), 4):
        made = generate_math(llm, slots[i : i + 4])
        typer.echo(f"math: {sum(q is not None for q in made)}/{len(made)} accepted")
    with session() as s:
        used = {q.passage_id for q in s.exec(select(Question).where(Question.section == "rw")).all()}
        fresh = [p for p in s.exec(select(Passage)).all() if p.id not in used]
    for p in fresh[:rw_sets]:
        skills = [rng.choice(skills_in("rw", d, article_only=True)).id for d in RW_DOMAINS]
        skills.append(rng.choice([s.id for s in skills_in("rw", article_only=True) if s.id not in skills]))
        made = generate_rw_set(llm, p, skills, ["easy", "medium", "medium", "hard", "medium"])
        typer.echo(f"rw: {len(made)}/5 accepted for '{p.title}'")
    _bank()


@app.command()
def stats(name: str):
    """Print a profile's projected score and weakest skills."""
    from .coach.sessions import insights, progress

    with session() as s:
        u = s.exec(select(User).where(User.name == name)).first()
    if not u:
        raise typer.BadParameter(f"No profile named {name!r}")
    p = progress(u.id)["projection"]
    typer.echo(f"Projected: {p['total']} ± {p['band']}  (Math {p['math']}, R&W {p['rw']})")
    weak = sorted([sk for sk in insights(u.id)["skills"] if sk["attempts"]], key=lambda x: -x["weakness"])[:5]
    for sk in weak:
        typer.echo(f"  {sk['name']}: {sk['accuracy']:.0%} over {sk['attempts']}")


ORIGIN_LABEL = {"seed": "original (seed set)", "official": "official (your imported PDFs)", "generated": "generated"}


@app.command()
def bank(
    origin: str = typer.Option(None, help="Only list questions with this origin: seed, official or generated."),
    section: str = typer.Option(None, help="math or rw"),
    limit: int = 30,
):
    """Show where the question bank's questions come from, optionally listing them."""
    with session() as s:
        rows = s.exec(select(Question.section, Question.origin, func.count()).group_by(Question.section, Question.origin)).all()
        typer.echo("Question bank by source:")
        for sec in ("math", "rw"):
            parts = [f"{n} {ORIGIN_LABEL.get(o, o)}" for se, o, n in rows if se == sec]
            typer.echo(f"  {'Math' if sec == 'math' else 'R&W':5} {', '.join(parts) or 'none'}")
        if origin is None and section is None:
            typer.echo("\nList questions with e.g.: sat-coach bank --origin generated --section math")
            return
        q = select(Question).order_by(Question.id.desc())
        if origin:
            q = q.where(Question.origin == origin)
        if section:
            q = q.where(Question.section == section)
        items = s.exec(q.limit(limit)).all()
        typer.echo("")
        for it in items:
            var = f" (variant of #{it.parent_id})" if it.parent_id else ""
            stem = " ".join(it.stem.split())[:70]
            typer.echo(f"#{it.id:<5} {it.origin:9} {it.section:4} {it.skill:24} {it.difficulty:6} "
                       f"{it.created_at:%Y-%m-%d}{var}  {stem}")


@app.command()
def preview(name: str):
    """Show what a profile's next session will contain and which questions will be generated.
    Makes no API calls and changes nothing."""
    from .coach.sessions import open_session, plan_session
    from .llm import get_llm
    from .taxonomy import SKILL_BY_ID

    with session() as s:
        u = s.exec(select(User).where(User.name == name)).first()
    if not u:
        raise typer.BadParameter(f"No profile named {name!r}")
    if open_session(u.id):
        typer.echo(f"{name} has an unfinished session; it will resume as is. Finish it to preview the next one.")
        return
    llm = get_llm()
    plan = plan_session(u.id, llm_available=llm is not None)
    typer.echo(f"Next session for {name}: {plan.kind}   generator: {llm.name if llm else 'none (offline)'}\n")
    label = {"bank": "from bank", "generate": "WILL BE GENERATED", "variant": "WILL BE GENERATED (variant of a missed question)",
             "fallback": "repeat/related (bank ran out)", "review": "review: the question you missed"}
    with session() as s:
        for i, m in enumerate(plan.math + plan.rw, 1):
            if i == len(plan.math) + 1:
                src = {"bank": "existing question set", "generate": "new questions WILL BE GENERATED",
                       "fallback": "reused set (no fresh article)"}.get(plan.rw_source, plan.rw_source)
                typer.echo(f"\nReading & Writing: {plan.passage_title or 'no article'} ({src})")
            where = label.get(m.source, m.source)
            if m.question_id and m.source != "generate":
                where += f": #{m.question_id} [{ORIGIN_LABEL.get(m.origin, m.origin)}]"
            typer.echo(f"  {i:2}. {SKILL_BY_ID[m.skill].name[:45]:45} {m.difficulty:6} {m.reason:8} {where}")
    n_gen = sum(m.source in ("generate", "variant") for m in plan.math + plan.rw)
    typer.echo(f"\n{n_gen} question(s) will be generated; the rest come from the existing bank.")


@users_app.command("add")
def users_add(name: str):
    with session() as s:
        s.add(User(name=name))
        s.commit()
    typer.echo(f"Added {name}")


@users_app.command("list")
def users_list():
    with session() as s:
        for u in s.exec(select(User).order_by(User.name)).all():
            typer.echo(f"{u.id}\t{u.name}")


def _bank():
    with session() as s:
        m = s.exec(select(func.count()).select_from(Question).where(Question.section == "math")).one()
        r = s.exec(select(func.count()).select_from(Question).where(Question.section == "rw")).one()
        p = s.exec(select(func.count()).select_from(Passage)).one()
    typer.echo(f"Bank: {m} math, {r} reading & writing, {p} passages.")


if __name__ == "__main__":
    app()
