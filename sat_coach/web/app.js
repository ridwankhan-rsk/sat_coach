const $app = document.getElementById("app");
const $nav = document.getElementById("nav");
const LETTERS = ["A", "B", "C", "D"];
const DOMAIN_NAMES = {
  algebra: "Algebra", advanced_math: "Advanced Math", psda: "Problem-Solving & Data Analysis",
  geometry_trig: "Geometry & Trigonometry", information_ideas: "Information & Ideas",
  craft_structure: "Craft & Structure", expression_ideas: "Expression of Ideas", conventions: "Standard English Conventions",
};
// Where a question came from: the committed seed set, the user's imported official PDFs,
// or the LLM. Generated variants of missed questions are labeled separately.
function sourceTag(q) {
  if (q.origin === "generated" && q.parent_id) return '<span class="tag src gen" title="Generated: a new question modeled on one you missed">Generated · variant</span>';
  if (q.origin === "generated") return '<span class="tag src gen" title="Written by the AI generator and checked before use">Generated</span>';
  if (q.origin === "official") return '<span class="tag src" title="Imported from your official practice PDFs">Official</span>';
  return '<span class="tag src" title="From the original seed question set">Original</span>';
}
const REASON_LABEL = { baseline: "Baseline", weak: "Focus skill", coverage: "Coverage", review: "Review" };

let me = null;
let practice = null; // { session, index, answers: {qid: {response, seconds}}, shownAt }

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, credentials: "same-origin", ...opts });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return res.json();
}

function renderMath(el = $app) {
  if (window.renderMathInElement) {
    window.renderMathInElement(el, { delimiters: [{ left: "$$", right: "$$", display: true }, { left: "$", right: "$", display: false }], throwOnError: false });
  }
}

function view(html) { $app.innerHTML = html; renderMath(); window.scrollTo(0, 0); }

// ---------------- Profiles
async function profilePicker() {
  $nav.hidden = true;
  const users = await api("/api/users");
  view(`
    <h1>Who's practicing?</h1>
    <p class="muted">Each profile keeps its own progress, scores, and review queue.</p>
    <div class="profiles">${users.map((u) => `<button data-id="${u.id}">${esc(u.name)}</button>`).join("") || '<span class="muted">No profiles yet.</span>'}</div>
    <form id="new" class="card" style="max-width:420px">
      <label class="small muted">New profile</label>
      <div style="display:flex;gap:8px;margin-top:6px"><input type="text" id="name" placeholder="Your name" required maxlength="40" style="flex:1"><button class="btn">Add</button></div>
      <p id="err" class="small" style="color:var(--bad)"></p>
    </form>`);
  $app.querySelectorAll(".profiles button").forEach((b) => b.onclick = async () => {
    await api("/api/login", { method: "POST", body: JSON.stringify({ user_id: +b.dataset.id }) });
    location.hash = "#/"; route();
  });
  document.getElementById("new").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/users", { method: "POST", body: JSON.stringify({ name: document.getElementById("name").value }) });
      location.hash = "#/"; route();
    } catch (err) { document.getElementById("err").textContent = err.message; }
  };
}

document.getElementById("switch").onclick = async () => {
  await api("/api/logout", { method: "POST" }); me = null; practice = null; location.hash = "#/"; route();
};

// ---------------- Home
async function home() {
  const [status, prog] = await Promise.all([api("/api/status"), api("/api/progress")]);
  const h = prog.history;
  const last = h[h.length - 1];
  const p = prog.projection;
  const first = h.length === 0;
  view(`
    <h1>Hi, ${esc(me.name)}</h1>
    <p class="muted">${first
      ? "Start with a baseline session: 8 math questions across all four domains, then one article with 5 Reading and Writing questions."
      : "Each session focuses on your weakest skills and brings back questions you missed, spaced out over time."}</p>
    ${status.llm ? "" : `<p class="notice">No API key set, so sessions use the built-in question bank only. Add a free <code>GEMINI_API_KEY</code> to <code>.env</code> to generate new questions.</p>`}
    <div class="grid" style="margin-top:16px">
      <div class="card">
        <div class="muted small">Projected score${first ? " (no data yet)" : ""}</div>
        <div class="score-big">${first ? "—" : p.total}</div>
        <div class="muted small">${first ? "" : `± ${p.band} · Math ${p.math} · R&W ${p.rw}`}</div>
      </div>
      <div class="card">
        <div class="muted small">Sessions completed</div>
        <div class="score-big">${h.length}</div>
        <div class="muted small">${last ? `Last: ${new Date(last.date).toLocaleDateString()}` : ""}</div>
      </div>
      <div class="card" style="display:flex;flex-direction:column;justify-content:center;gap:10px">
        <button class="btn" id="start">${me.open_session ? "Resume session" : first ? "Start baseline session" : "Start next session"}</button>
        <span class="muted small">${bankLine("Math", status.bank.by_origin.math)}<br>${bankLine("R&W", status.bank.by_origin.rw)} · ${status.bank.passages} articles${status.llm ? `<br>Generator: ${esc(status.llm)}` : ""}</span>
      </div>
    </div>`);
  document.getElementById("start").onclick = startSession;
}

function bankLine(label, counts = {}) {
  const names = { seed: "original", official: "official", generated: "generated" };
  const parts = ["seed", "official", "generated"].filter((o) => counts[o]).map((o) => `${counts[o]} ${names[o]}`);
  return `${label}: ${parts.join(" · ") || "none"}`;
}

async function startSession() {
  view(`<p><span class="spinner"></span>Building your session… new questions are generated and checked, which can take up to a minute.</p>`);
  try {
    const session = await api("/api/sessions", { method: "POST" });
    practice = { session, index: 0, answers: {}, shownAt: Date.now() };
    location.hash = `#/session/${session.id}`;
  } catch (e) {
    view(`<p class="notice">Couldn't start a session: ${esc(e.message)}</p><p><a class="btn secondary" href="#/">Back</a></p>`);
  }
}

// ---------------- Practice
async function sessionPage(id) {
  if (!practice || practice.session.id !== +id) {
    const session = await api(`/api/sessions/${id}`);
    if (session.completed) return resultsPage(session);
    practice = { session, index: 0, answers: {}, shownAt: Date.now() };
  }
  renderQuestion();
}

function recordTime() {
  const q = practice.session.questions[practice.index];
  const a = practice.answers[q.id] || (practice.answers[q.id] = { response: "", seconds: 0 });
  a.seconds += (Date.now() - practice.shownAt) / 1000;
  practice.shownAt = Date.now();
}

function renderQuestion() {
  const { session, index, answers } = practice;
  const qs = session.questions;
  const q = qs[index];
  const passage = q.passage_id ? session.passages[q.passage_id] : null;
  const firstRw = qs.findIndex((x) => x.section === "rw");
  const bar = qs.map((x, i) => `${i === firstRw && i > 0 ? '<span class="sep"></span>' : ""}<button data-i="${i}" class="${answers[x.id]?.response ? "answered" : ""} ${i === index ? "current" : ""}">${i + 1}</button>`).join("");
  const answer = answers[q.id]?.response || "";
  const body = q.qtype === "mcq"
    ? `<div class="choices">${q.choices.map((c, i) => `<button class="choice ${answer === LETTERS[i] ? "selected" : ""}" data-l="${LETTERS[i]}"><span class="letter">${LETTERS[i]}</span><span>${esc(c)}</span></button>`).join("")}</div>`
    : `<label class="small muted">Your answer (number or fraction like 7/2)</label><br><input type="text" id="spr" value="${esc(answer)}" style="margin-top:6px;width:200px" autocomplete="off">`;
  const passageHtml = passage ? `<div class="card passage">
      <div class="src muted">${esc(passage.title)} · ${esc(passage.author)}${passage.year ? `, ${esc(passage.year)}` : ""}</div>
      <h2 style="margin-top:8px">${esc(passage.title.replace(/ \(excerpt \d+\)$/, ""))}</h2>
      ${(q.passage_text || passage.text).split(/\n\s*\n/).map((p) => `<p>${esc(p)}</p>`).join("")}
      <div class="src muted">${esc(passage.license)}${passage.source_url ? ` · <a href="${esc(passage.source_url)}" target="_blank" rel="noopener">source</a>` : ""}</div>
    </div>` : "";
  const allAnswered = qs.every((x) => answers[x.id]?.response);
  view(`
    <div class="qbar">${bar}</div>
    <div class="practice ${passage ? "with-passage" : ""}">
      ${passageHtml}
      <div class="card">
        <div class="meta">
          <span class="tag">${q.section === "math" ? "Math" : "Reading & Writing"}</span>
          <span class="tag">${esc(q.skill_name)}</span>
          <span class="tag">${esc(q.difficulty)}</span>
          ${q.reason === "review" ? '<span class="tag">Review</span>' : ""}
          ${sourceTag(q)}
        </div>
        ${q.excerpt ? `<div class="excerpt">${esc(q.excerpt)}</div>` : ""}
        <p class="stem">${esc(q.stem)}</p>
        ${body}
        <div class="nav-row">
          <button class="btn secondary" id="prev" ${index === 0 ? "disabled" : ""}>Back</button>
          ${index < qs.length - 1 ? '<button class="btn" id="next">Next</button>' : `<button class="btn" id="submit">Submit session</button>`}
        </div>
        ${index === qs.length - 1 && !allAnswered ? '<p class="muted small">Unanswered questions count as incorrect.</p>' : ""}
      </div>
    </div>`);

  const go = (i) => { recordTime(); practice.index = i; renderQuestion(); };
  $app.querySelectorAll(".qbar button[data-i]").forEach((b) => b.onclick = () => go(+b.dataset.i));
  $app.querySelectorAll(".choice").forEach((b) => b.onclick = () => {
    (practice.answers[q.id] ||= { response: "", seconds: 0 }).response = b.dataset.l;
    $app.querySelectorAll(".choice").forEach((x) => x.classList.toggle("selected", x === b));
    $app.querySelector(`.qbar button[data-i="${index}"]`).classList.add("answered");
  });
  const spr = document.getElementById("spr");
  if (spr) { spr.oninput = () => { (practice.answers[q.id] ||= { response: "", seconds: 0 }).response = spr.value.trim(); }; spr.focus(); }
  document.getElementById("prev").onclick = () => index > 0 && go(index - 1);
  const next = document.getElementById("next");
  if (next) next.onclick = () => go(index + 1);
  const submit = document.getElementById("submit");
  if (submit) submit.onclick = submitSession;
}

async function submitSession() {
  recordTime();
  const unanswered = practice.session.questions.filter((q) => !practice.answers[q.id]?.response).length;
  if (unanswered && !confirm(`${unanswered} question(s) unanswered. Submit anyway?`)) return;
  view(`<p><span class="spinner"></span>Grading…</p>`);
  const res = await api(`/api/sessions/${practice.session.id}/submit`, { method: "POST", body: JSON.stringify({ answers: practice.answers }) });
  practice = null; me.open_session = null;
  resultsPage(res);
}

// ---------------- Results
function rationaleHtml(q) {
  if (q.qtype === "spr") {
    return `<div class="rat correct"><b>Correct answer: ${esc(q.answer.split("|").join(" or "))}.</b> ${esc(q.rationale.correct || "")}</div>`;
  }
  const order = [q.answer, ...LETTERS.filter((l) => l !== q.answer)];
  const main = order.filter((l) => l === q.answer || l === q.response);
  const rest = order.filter((l) => !main.includes(l));
  const block = (l) => `<div class="rat ${l === q.answer ? "correct" : l === q.response ? "chosen" : ""}"><b>${l}) ${esc(q.choices[LETTERS.indexOf(l)])}</b><br>${esc(q.rationale[l] || "")}</div>`;
  return main.map(block).join("") + (rest.length ? `<details><summary>Why the other choices are wrong</summary>${rest.map(block).join("")}</details>` : "");
}

function resultsPage(res) {
  const qs = res.questions;
  const right = qs.filter((q) => q.correct).length;
  const bySection = (sec) => { const xs = qs.filter((q) => q.section === sec); return `${xs.filter((q) => q.correct).length}/${xs.length}`; };
  const byDomain = {};
  qs.forEach((q) => { (byDomain[q.domain] ||= [0, 0]); byDomain[q.domain][1]++; if (q.correct) byDomain[q.domain][0]++; });
  const score = res.projection || res.score;
  const delta = res.previous_total != null && score ? score.total - res.previous_total : null;
  view(`
    <h1>Session results</h1>
    <div class="grid">
      <div class="card"><div class="muted small">Correct</div><div class="score-big">${right}/${qs.length}</div><div class="muted small">Math ${bySection("math")} · R&W ${bySection("rw")}</div>
        <div class="muted small">${qs.filter((q) => q.origin === "generated").length} generated · ${qs.filter((q) => q.origin !== "generated").length} from the original/official set</div></div>
      <div class="card"><div class="muted small">Projected score</div>
        <div class="score-row"><span class="score-big">${score.total}</span>${delta != null ? `<span class="delta ${delta >= 0 ? "up" : "down"}">${delta >= 0 ? "+" : ""}${delta} since last session</span>` : ""}</div>
        <div class="muted small">± ${score.band} · Math ${score.math} · R&W ${score.rw} · an estimate from your practice, not an official score</div></div>
      <div class="card"><div class="muted small">By domain</div>${Object.entries(byDomain).map(([d, [c, n]]) => `<div class="small">${DOMAIN_NAMES[d]}: <b>${c}/${n}</b></div>`).join("")}</div>
    </div>
    <h2>Question by question</h2>
    <p class="muted small">For each miss, tag why it happened. It helps tell concept gaps apart from careless slips.</p>
    ${qs.map((q, i) => `
      <div class="card result ${q.correct ? "ok" : "miss"}">
        <div class="meta"><span class="tag">Q${i + 1}</span><span class="tag">${esc(q.skill_name)}</span><span class="tag">${esc(q.difficulty)}</span>${q.reason === "review" ? '<span class="tag">Review</span>' : ""}${sourceTag(q)}</div>
        ${q.passage_id && res.passages[q.passage_id] ? `<div class="muted small">From “${esc(res.passages[q.passage_id].title)}”</div>` : ""}
        ${q.excerpt ? `<div class="excerpt">${esc(q.excerpt)}</div>` : ""}
        <p class="stem">${esc(q.stem)}</p>
        <p class="verdict ${q.correct ? "ok" : "miss"}">${q.correct ? "Correct" : q.response ? `Incorrect. You answered ${esc(q.response)}.` : "Not answered."}</p>
        ${rationaleHtml(q)}
        ${!q.correct && q.attempt_id ? `<div class="reasons" data-a="${q.attempt_id}"><span class="muted">Why I missed it:</span>
          ${["concept", "careless", "time"].map((r) => `<button data-r="${r}" class="${q.miss_reason === r ? "on" : ""}">${{ concept: "Didn't know how", careless: "Careless mistake", time: "Ran out of time" }[r]}</button>`).join("")}</div>` : ""}
      </div>`).join("")}
    <p><a class="btn" href="#/">Done</a> <a class="btn secondary" href="#/progress">See progress</a></p>`);
  $app.querySelectorAll(".reasons").forEach((box) => box.querySelectorAll("button").forEach((b) => b.onclick = async () => {
    await api(`/api/attempts/${box.dataset.a}/reason`, { method: "POST", body: JSON.stringify({ reason: b.dataset.r }) });
    box.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  }));
}

// ---------------- Progress
let chart;
async function progressPage() {
  const prog = await api("/api/progress");
  const h = prog.history;
  const doms = Object.entries(prog.domains);
  view(`
    <h1>Progress</h1>
    ${h.length ? "" : '<p class="muted">Complete a session to see your projected score.</p>'}
    <div class="card"><canvas id="chart" height="110"></canvas></div>
    <h2>Domains</h2>
    <p class="muted small">Estimated chance of getting a medium-difficulty question right.</p>
    <div class="grid">
      ${["math", "rw"].map((sec) => `<div class="card"><b>${sec === "math" ? "Math" : "Reading & Writing"}</b>
        ${doms.filter(([, v]) => v.section === sec).map(([d, v]) => `<div style="margin-top:10px"><div class="small">${DOMAIN_NAMES[d]} <span class="muted">${Math.round(v.p_medium * 100)}%</span></div><div class="bar"><span style="width:${v.p_medium * 100}%"></span></div></div>`).join("")}
      </div>`).join("")}
    </div>`);
  if (!h.length || !window.Chart) return;
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue("--muted").trim(), line = css.getPropertyValue("--line").trim(), accent = css.getPropertyValue("--accent").trim();
  chart?.destroy();
  chart = new Chart(document.getElementById("chart"), {
    type: "line",
    data: {
      labels: h.map((x, i) => `#${i + 1}`),
      datasets: [
        { label: "Total", data: h.map((x) => x.total), borderColor: accent, backgroundColor: accent, tension: 0.25 },
        { label: "Band low", data: h.map((x) => x.total - x.band), borderColor: "transparent", pointRadius: 0, fill: false },
        { label: "Band high", data: h.map((x) => x.total + x.band), borderColor: "transparent", backgroundColor: accent + "22", pointRadius: 0, fill: "-1" },
      ],
    },
    options: {
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => c.datasetIndex === 0 ? `Total ${c.raw} (Math ${h[c.dataIndex].math}, R&W ${h[c.dataIndex].rw})` : null } } },
      scales: { y: { suggestedMin: 400, suggestedMax: 1600, ticks: { color: ink }, grid: { color: line } }, x: { ticks: { color: ink }, grid: { display: false } } },
    },
  });
}

// ---------------- Insights
function accCell(v) {
  if (!v) return '<span class="cell muted">–</span>';
  const a = v.accuracy;
  const bg = a >= 0.75 ? "var(--good-bg)" : a >= 0.5 ? "var(--warn-bg)" : "var(--bad-bg)";
  return `<span class="cell" style="background:${bg}" title="${v.n} attempt(s)">${Math.round(a * 100)}%</span>`;
}

async function insightsPage() {
  const ins = await api("/api/insights");
  // Weakest tested skills first; skills with no attempts yet go to the bottom.
  const rows = (sec) => ins.skills.filter((s) => s.section === sec)
    .sort((a, b) => (!a.attempts - !b.attempts) || (b.weakness - a.weakness));
  const table = (sec) => `<div class="card table-wrap"><table>
    <tr><th>Skill</th><th>Domain</th><th>Attempts</th><th>Easy</th><th>Medium</th><th>Hard</th><th>Ability</th></tr>
    ${rows(sec).map((s) => `<tr><td>${esc(s.name)}</td><td class="muted">${DOMAIN_NAMES[s.domain]}</td><td>${s.attempts}</td>
      <td>${accCell(s.by_difficulty.easy)}</td><td>${accCell(s.by_difficulty.medium)}</td><td>${accCell(s.by_difficulty.hard)}</td>
      <td class="muted">${s.attempts ? s.theta.toFixed(2) : "–"}</td></tr>`).join("")}
  </table></div>`;
  const reasons = ins.miss_reasons;
  view(`
    <h1>Insights</h1>
    <p class="muted">What the coach uses to pick your questions. Skills are sorted weakest first.</p>
    <h2>Review queue</h2>
    ${ins.review_queue.length ? `<div class="card table-wrap"><table><tr><th>Skill</th><th>Times missed</th><th>Box</th><th>Comes back</th></tr>
      ${ins.review_queue.sort((a, b) => a.due_in_sessions - b.due_in_sessions).map((r) => `<tr><td>${esc(r.skill_name)}</td><td>${r.times_missed}</td><td>${r.box} of 4</td><td>${r.due_in_sessions === 0 ? "next session" : `in ${r.due_in_sessions} session(s)`}</td></tr>`).join("")}
    </table></div>` : '<p class="muted">Nothing to review yet.</p>'}
    ${Object.keys(reasons).length ? `<p class="small">Why misses happen: ${Object.entries(reasons).map(([k, v]) => `${k} ${v}`).join(" · ")}</p>` : ""}
    <h2>Most-missed questions</h2>
    ${ins.hardest.length ? `<div class="card table-wrap"><table><tr><th>Missed</th><th>Skill</th><th>Question</th></tr>${ins.hardest.map((h) => `<tr><td>${h.times_missed}×</td><td>${esc(h.skill_name)}</td><td class="muted">${esc(h.stem)}…</td></tr>`).join("")}</table></div>` : '<p class="muted">No misses yet.</p>'}
    <h2>Math skills</h2>${table("math")}
    <h2>Reading & Writing skills</h2>${table("rw")}`);
}

// ---------------- Router
async function route() {
  try {
    if (!me) me = await api("/api/me");
  } catch (e) {
    if (e.status === 401) return profilePicker();
    return view(`<p class="notice">${esc(e.message)}</p>`);
  }
  $nav.hidden = false;
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/session\/(\d+)/);
  try {
    if (m) return await sessionPage(m[1]);
    if (hash === "#/progress") return await progressPage();
    if (hash === "#/insights") return await insightsPage();
    me = await api("/api/me");
    return await home();
  } catch (e) {
    if (e.status === 401) { me = null; return profilePicker(); }
    view(`<p class="notice">${esc(e.message)}</p>`);
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("load", route);
