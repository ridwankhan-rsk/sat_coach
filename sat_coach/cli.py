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
