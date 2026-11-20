// The catalog's web page. It reads the HTTP API and renders with DOM nodes only: every value from the catalog is set
// as text, never parsed as HTML, because descriptions and names are whatever people and importers registered.
"use strict";

const main = document.getElementById("main");
const tooltip = document.getElementById("tooltip");
const SVG = "http://www.w3.org/2000/svg";

// ---- small helpers --------------------------------------------------------------------------------------------
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function s(tag, attrs, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined) continue;
    if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await response.json().catch(() => ({ error: { message: `${response.status} ${response.statusText}` } }));
  if (!response.ok) throw Object.assign(new Error(body.error?.message || response.statusText), { code: body.error?.code, status: response.status });
  return body;
}

const enc = encodeURIComponent;
const entryHref = (key) => `#/entry/${enc(key)}`;
const short = (key) => String(key).replace(/^(table|column|project):/, "");
const kindOf = (key) => String(key).split(":", 1)[0];
function when(iso) {
  if (!iso) return "—";
  const at = new Date(iso);
  const sameYear = at.getFullYear() === new Date().getFullYear();
  return h("span", { class: "nowrap", title: at.toISOString() },
    at.toLocaleString(undefined, { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }), hour: "numeric", minute: "2-digit" }));
}
const plural = (n, word, many = `${word}s`) => `${n.toLocaleString()} ${n === 1 ? word : many}`;

function kindBadge(kind) {
  return h("span", { class: `kind ${kind}` }, kind);
}

function entryLink(key, text) {
  return h("a", { href: entryHref(key) }, text ?? short(key));
}

function stateBadge(state) {
  const value = String(state || "UNKNOWN");
  const good = /^(COMPLETE|SUCCEEDED|PIPELINE_STATE_SUCCEEDED|DONE)$/.test(value);
  const bad = /(FAIL|ABORT|CANCEL)/.test(value);
  return h("span", { class: `status ${good ? "good" : bad ? "critical" : ""}` }, good ? "✓ " : bad ? "✕ " : "◷ ", value.replace("PIPELINE_STATE_", ""));
}

function changedBadge() {
  return h("span", { class: "status warning", title: "Redefined after this link was recorded" }, "△ changed since");
}

function facts(pairs) {
  return h("dl", { class: "facts" }, pairs.filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

function table(headers, rows, empty = "Nothing recorded.") {
  if (!rows.length) return h("p", { class: "muted" }, empty);
  return h("table", {}, h("thead", {}, h("tr", {}, headers.map((x) => h("th", { scope: "col" }, x)))),
    h("tbody", {}, rows.map((cells) => h("tr", {}, cells.map((c) => h("td", {}, c))))));
}

function showTooltip(event, lines) {
  tooltip.replaceChildren(...lines.map((line, i) => h("div", { class: i === 0 ? "t" : "" }, line)));
  tooltip.hidden = false;
  const x = Math.min(event.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8);
  const y = Math.min(event.clientY + 14, window.innerHeight - tooltip.offsetHeight - 8);
  tooltip.style.left = `${x}px`;
  tooltip.style.top = `${y}px`;
}
const hideTooltip = () => { tooltip.hidden = true; };

function render(...nodes) {
  hideTooltip();
  main.replaceChildren(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
  main.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

function failure(error) {
  return h("div", { class: "error" }, h("strong", {}, error.status === 404 ? "Not found. " : "Could not load this. "), error.message);
}

// ---- the lineage graph ----------------------------------------------------------------------------------------
// Upstream to the left, downstream to the right, one column per hop. Each node names its kind in words beside its
// color, so the color never carries identity alone; the tables under the graph say the same in text.
function lineageGraph(lineage) {
  const focus = { ...lineage, depth: 0, side: 0 };
  const nodes = [focus,
    ...(lineage.upstream || []).map((n) => ({ ...n, side: -1 })),
    ...(lineage.downstream || []).map((n) => ({ ...n, side: 1 }))];
  if (nodes.length === 1) return h("p", { class: "muted" }, "No lineage is recorded for this entry.");
  const MAX = 80;
  const shown = nodes.slice(0, MAX);
  const W = 240, H = 46, GX = 64, GY = 12, PAD = 8;
  const columns = new Map();
  for (const n of shown) {
    n.col = n.side * n.depth;
    if (!columns.has(n.col)) columns.set(n.col, []);
    columns.get(n.col).push(n);
  }
  const cols = [...columns.keys()].sort((a, b) => a - b);
  const place = new Map();
  // order each column by where its neighbour toward the focus sits, so edges cross less
  for (const col of [...cols].sort((a, b) => Math.abs(a) - Math.abs(b))) {
    const list = columns.get(col);
    list.sort((a, b) => ((place.get(a.via)?.row ?? 0) - (place.get(b.via)?.row ?? 0)) || a.title.localeCompare(b.title));
    list.forEach((n, row) => place.set(n.key, { row, col }));
  }
  const tallest = Math.max(...[...columns.values()].map((l) => l.length));
  const height = tallest * (H + GY) - GY + PAD * 2;
  const width = cols.length * (W + GX) - GX + PAD * 2;
  const x = (col) => PAD + cols.indexOf(col) * (W + GX);
  const y = (n) => {
    const list = columns.get(n.col);
    const offset = ((tallest - list.length) * (H + GY)) / 2;
    return PAD + offset + place.get(n.key).row * (H + GY);
  };
  const byKey = new Map(shown.map((n) => [n.key, n]));
  const edges = [];
  for (const n of shown) {
    const other = byKey.get(n.via);
    if (!other || n === focus) continue;
    const [from, to] = n.side < 0 ? [n, other] : [other, n];
    const x1 = x(from.col) + W, y1 = y(from) + H / 2, x2 = x(to.col), y2 = y(to) + H / 2, mid = (x1 + x2) / 2;
    edges.push(s("path", { class: `edge${n.changed_since ? " changed" : ""}`, d: `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}` }));
  }
  const boxes = shown.map((n) => {
    // the last two parts name a table (schema.table) or a column (table.column); the whole name is in the tooltip
    const tail = n.kind === "project" ? n.title : n.title.split(".").slice(-2).join(".");
    const label = tail.length > 34 ? `${tail.slice(0, 33)}…` : tail;
    const tip = [n.title, `${n.kind}${n.layer ? ` · ${n.layer}` : ""}${n.system ? ` · ${n.system}` : ""}${n.dtype ? ` · ${n.dtype}` : ""}`,
      ...(n.description ? [n.description.length > 200 ? `${n.description.slice(0, 200)}…` : n.description] : []),
      ...(n.changed_since ? ["△ redefined after this link was recorded"] : [])];
    const go = () => { location.hash = entryHref(n.key); };
    return s("g", { class: `node${n === focus ? " focus" : ""}`, transform: `translate(${x(n.col)},${y(n)})`, tabindex: 0, role: "link",
      "aria-label": `${n.kind} ${n.title}`, onclick: go, onkeydown: (e) => { if (e.key === "Enter") go(); },
      onmousemove: (e) => showTooltip(e, tip), onmouseleave: hideTooltip },
    s("rect", { class: "box", width: W, height: H, rx: 6 }),
    s("rect", { x: 0, y: 0, width: 5, height: H, rx: 2, fill: `var(--${n.kind})` }),
    s("text", { class: "kindlabel", x: 14, y: 17 }, n.kind.toUpperCase() + (n.layer ? ` · ${n.layer}` : "")),
    s("text", { x: 14, y: 35 }, label));
  });
  const svg = s("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": `Lineage of ${lineage.title}: ${plural((lineage.upstream || []).length, "upstream entry", "upstream entries")}, ${plural((lineage.downstream || []).length, "downstream entry", "downstream entries")}` },
  edges, boxes);
  return h("div", {},
    h("div", { class: "card graph" }, svg),
    h("div", { class: "legend muted" }, kindBadge("table"), kindBadge("column"), kindBadge("project"),
      h("span", {}, "Upstream on the left, downstream on the right. A dashed amber edge: the input was redefined after the link was recorded."),
      nodes.length > MAX ? h("span", {}, `Showing ${MAX} of ${nodes.length}; the lists below have all of them.`) : null));
}

function lineageLists(lineage) {
  const row = (n) => [h("span", {}, "".padStart((n.depth - 1) * 2, " "), entryLink(n.key, n.title)), kindBadge(n.kind), String(n.depth),
    n.changed_since ? changedBadge() : ""];
  return h("div", { class: "two" },
    h("div", {}, h("h2", {}, "Built from"), table(["Entry", "Kind", "Hops", ""], (lineage.upstream || []).map(row), "Nothing upstream.")),
    h("div", {}, h("h2", {}, "Feeds"), table(["Entry", "Kind", "Hops", ""], (lineage.downstream || []).map(row), "Nothing downstream.")));
}

// ---- pages ----------------------------------------------------------------------------------------------------
async function overviewPage() {
  const [overview, models, runs] = await Promise.all([api("/v1/overview"), api("/v1/models"), api("/v1/runs?limit=8")]);
  const tile = (value, label, href) => h(href ? "a" : "div", { class: "card tile", href }, h("div", { class: "value" }, value.toLocaleString()), h("div", { class: "label" }, label));
  render(
    h("h1", {}, "Overview"),
    h("p", { class: "sub" }, "What data exists, how it connects, and which projects and models use it."),
    h("div", { class: "grid" },
      tile(overview.counts.table, "tables", "#/search?kind=table"), tile(overview.counts.column, "columns", "#/search?kind=column"),
      tile(overview.counts.project, "projects", "#/search?kind=project"), tile(models.length, "model versions", "#/models")),
    h("h2", {}, "Changed after a project used it"),
    table(["Project", "Uses", ""], overview.changed_since_used.map((c) => [entryLink(c.project), entryLink(c.key), changedBadge()]),
      "Every project uses data as it is defined now."),
    h("h2", {}, "Recent model versions"),
    table(["Model", "Version", "Trained on", "Recorded"], models.slice(0, 8).map((m) => [m.model,
      h("a", { href: `#/model/${enc(m.model)}/${enc(m.version)}` }, m.version),
      h("a", { href: `#/dataset/${enc(m.dataset_version)}` }, m.dataset_version), when(m.recorded_at)]), "No model version is recorded yet."),
    h("h2", {}, "Recent runs"), runsTable(runs),
    h("p", { class: "muted" }, `Database ${overview.database}`));
}

async function searchPage(params) {
  const q = params.get("q") || "", kind = params.get("kind") || "";
  document.getElementById("q").value = q;
  const results = await api(`/v1/search?q=${enc(q)}&limit=200${kind ? `&kind=${enc(kind)}` : ""}`);
  const select = h("select", { "aria-label": "Kind", onchange: (e) => { location.hash = `#/search?q=${enc(q)}${e.target.value ? `&kind=${e.target.value}` : ""}`; } },
    ["", "table", "column", "project"].map((k) => h("option", { value: k, selected: k === kind }, k ? `${k}s` : "everything")));
  render(
    h("h1", {}, q ? `Results for “${q}”` : `All ${kind ? `${kind}s` : "entries"}`),
    h("div", { class: "controls" }, h("span", { class: "muted" }, plural(results.length, "result")), select),
    results.length ? h("ul", { class: "results" }, results.map((r) => h("li", {},
      h("div", {}, kindBadge(r.kind), " ", h("a", { class: "title", href: entryHref(r.key) }, r.title),
        r.layer ? h("span", { class: "muted" }, ` · ${r.layer}`) : null, r.dtype ? h("span", { class: "muted" }, ` · ${r.dtype}`) : null,
        r.used_by ? h("span", { class: "muted" }, ` · used by ${plural(r.used_by, "project")}`) : null),
      r.description ? h("p", {}, r.description) : null))) : h("p", { class: "muted" }, "Nothing matches every word."));
}

async function entryPage(key) {
  const entry = await api(`/v1/entries/${enc(key)}`);
  const parts = [
    h("div", {}, kindBadge(entry.kind)),
    h("h1", {}, entry.title),
    entry.description ? h("p", { class: "sub" }, entry.description) : h("p", { class: "sub muted" }, "No description yet."),
    h("div", { class: "card" }, facts([
      ["Key", h("code", {}, entry.key)],
      ["Table", entry.table ? entryLink(entry.table) : null],
      ["Type", entry.dtype], ["Definition", entry.definition ? h("code", {}, entry.definition) : null],
      ["Layer", entry.layer], ["System", entry.system], ["Owner", entry.owner], ["Status", entry.status],
      ["Repository", entry.repository],
      ["Tags", (entry.tags || []).join(", ")],
      ["Revision", [`${entry.revision} by ${entry.registered_by}, `, when(entry.at)]],
      ["Sources", sources(entry.source)],
    ])),
  ];
  if (entry.kind === "project") {
    parts.push(h("h2", {}, "Uses"),
      table(["Entry", "Kind", "For", ""], entry.uses.map((u) => [entryLink(u.key, u.title), kindBadge(u.kind), u.purpose || "", u.changed_since ? changedBadge() : ""]),
        "This project has recorded no use of data yet."));
  } else {
    const lineage = await api(`/v1/entries/${enc(entry.key)}/lineage?direction=both`);
    parts.push(h("h2", {}, "Lineage"), lineageGraph(lineage), lineageLists(lineage));
    if (entry.kind === "table") {
      parts.push(h("h2", {}, `Columns (${entry.columns.length})`),
        table(["Column", "Type", "Description"], entry.columns.map((c) => [entryLink(c.key, c.title.split(".").pop()), c.dtype || "", c.description || ""]),
          "No columns are registered."));
    }
    parts.push(h("h2", {}, "Used by"),
      table(["Project", "How", "Through", "For", ""], entry.used_by.map((u) => [entryLink(u.project), u.how, u.through === entry.key ? "this entry" : entryLink(u.through),
        u.purpose || "", u.changed_since ? changedBadge() : ""]), "No project depends on this."));
    if (entry.kind === "table") parts.push(await historySection(entry.key));
  }
  parts.push(h("h2", {}, "History"), table(["Revision", "By", "When"], entry.history.map((r) => [h("code", {}, r.revision), r.registered_by, when(r.at)])));
  if (Object.keys(entry.source || {}).length) {
    parts.push(h("details", {}, h("summary", { class: "muted" }, "Where this came from, as the importers recorded it"),
      h("div", { class: "card" }, h("pre", { class: "mono" }, JSON.stringify(entry.source, null, 2)))));
  }
  render(...parts);
}

// dbt records {tool: "dbt", ...}; the BigQuery, Dataform and OpenLineage importers each keep their details under their name
function sources(source) {
  const named = Object.keys(source || {}).filter((k) => source[k] && typeof source[k] === "object" && k !== "code");
  const found = [...new Set([...(typeof source?.tool === "string" ? [source.tool] : []), ...named])];
  return found.length ? found.join(", ") : "registered by hand";
}

async function historySection(key) {
  try {
    const history = await api(`/v1/query-history?table=${enc(short(key))}&since=30d&limit=20`);
    if (!history.jobs.length) return h("div", {});
    return h("div", {}, h("h2", {}, `Query jobs in the last 30 days (${history.totals.jobs})`),
      table(["Job", "Read or write", "State", "Created"], history.jobs.map((j) => [h("code", {}, j.job_id), j.roles.join(", "), stateBadge(j.state), when(j.created)])),
      history.more ? h("p", { class: "muted" }, "More jobs than shown.") : null);
  } catch {
    return h("div", {});
  }
}

function step(label, what, ...details) {
  return h("li", {}, h("div", { class: "step" }, label), h("div", { class: "what" }, what), details.filter(Boolean).map((d) => h("div", { class: "muted" }, d)));
}

function missingList(missing) {
  return [h("h2", {}, missing.length ? `Not established (${missing.length})` : "Not established"),
    missing.length ? missing.map((m) => h("div", { class: "missing" }, "△ ", m)) : h("p", { class: "status good" }, "✓ Every link of this trace is recorded.")];
}

async function modelPage(name, version) {
  const trace = await api(`/v1/models/${enc(name)}/versions/${enc(version)}/trace`);
  const p = trace.produced_by, read = trace.read;
  render(
    h("div", {}, kindBadge("project"), h("span", { class: "muted" }, " model version")),
    h("h1", {}, `${trace.model} ${trace.version}`),
    h("p", { class: "sub" }, "From the training task back to the raw column behind each input, as pinned when the model was trained."),
    h("ol", { class: "trace" },
      step("Model artifact", trace.artifact.uri, `generation ${trace.artifact.generation}`, `sha256 ${trace.artifact.sha256}`),
      step("Trained by", `${p.pipeline_run.split("/").pop()} / task ${p.task}`, p.pipeline_run,
        p.training_commit ? `training code at commit ${p.training_commit}` : "training code commit not recorded",
        p.prediction_cutoff ? `prediction cutoff ${p.prediction_cutoff}` : null),
      step("Read dataset version", h("a", { href: `#/dataset/${enc(read.dataset_version)}` }, read.dataset_version),
        h("span", {}, "a snapshot of ", entryLink(`table:${read.snapshot_of}`)))),
    h("h2", {}, `Inputs (${trace.inputs.length})`),
    trace.inputs.map((i) => h("div", { class: "card", style: null },
      h("div", {}, h("strong", {}, i.field), h("span", { class: "muted" }, ` · ${i.role}${i.position !== null ? ` #${i.position}` : ""}`),
        i.lineage !== "resolved" ? h("span", { class: "status warning" }, ` △ lineage ${i.lineage}`) : null),
      h("ol", { class: "trace", style: null },
        step("Expression", h("code", {}, i.expression ?? "?"), i.column ? h("span", {}, "column ", entryLink(`column:${i.column}`)) : null),
        step("Built by", i.produced_by.dataform_invocation ? i.produced_by.dataform_invocation.split("/").pop() : "?",
          i.produced_by.dataform_invocation, i.produced_by.state ? h("span", {}, stateBadge(i.produced_by.state)) : null),
        step("Executed as", i.executed_as ? i.executed_as.job_id : "an unknown job",
          i.executed_as ? `BigQuery job in ${i.executed_as.project}, ${i.executed_as.location} · ${i.executed_as.statement_type}` : null),
        step("Defined in", i.defined_in.file || "?", i.defined_in.commit ? `commit ${i.defined_in.commit}` : "no git commit"),
        step("From raw columns", i.derived_from.length ? i.derived_from.map((c, n) => [n ? ", " : "", entryLink(`column:${c}`, c)]) : "?")))),
    ...missingList(trace.missing));
}

async function datasetPage(reference) {
  const d = await api(`/v1/datasets/${enc(reference)}/trace`);
  render(
    h("div", {}, h("span", { class: "kind table" }, "dataset version")),
    h("h1", {}, d.reference),
    h("p", { class: "sub" }, "A snapshot handed on by a build, with each field pinned to the definition that build used."),
    h("ol", { class: "trace" },
      step("Snapshot of", entryLink(`table:${d.snapshot_of}`), `taken ${d.snapshot_time}`),
      step("Produced by", d.produced_by.dataform_invocation ? d.produced_by.dataform_invocation.split("/").pop() : "?", d.produced_by.dataform_invocation,
        h("span", {}, stateBadge(d.produced_by.state), " ", when(d.produced_by.started), " → ", when(d.produced_by.ended))),
      step("Executed as", d.executed_as ? d.executed_as.job_id : "an unknown job",
        d.executed_as ? `BigQuery job in ${d.executed_as.project}, ${d.executed_as.location} · ${d.executed_as.statement_type} · ${(d.executed_as.billed_bytes ?? 0).toLocaleString()} bytes billed` : null),
      step("Defined in", d.defined_in.file || "?", d.defined_in.commit ? `commit ${d.defined_in.commit}` : "no git commit")),
    h("h2", {}, `Fields (${d.columns.length})`),
    table(["Field", "Expression", "From", "Lineage"], d.columns.map((c) => [entryLink(`column:${c.column}`, c.field), h("code", {}, c.expression ?? "?"),
      c.derived_from.map((x, n) => [n ? ", " : "", entryLink(`column:${x.column}`, x.column), x.changed_since ? [" ", changedBadge()] : ""]),
      c.lineage === "resolved" ? h("span", { class: "status good" }, "✓ resolved") : h("span", { class: "status warning" }, `△ ${c.lineage}`)])),
    h("h2", {}, "Checks"),
    facts([["Snapshot copies the target", d.checks.snapshot_copies_target ? "yes" : "no"], ["Taken after the build ended", d.checks.snapshot_after_build ? "yes" : "no"],
      ["Written again before the snapshot", d.checks.written_again_before_snapshot === null ? "unknown: history does not reach it" : d.checks.written_again_before_snapshot ? "yes" : "no"],
      ["Recorded", [`by ${d.recorded_by}, `, when(d.recorded_at)]]]),
    ...missingList(d.missing));
}

async function modelsPage() {
  const models = await api("/v1/models");
  render(h("h1", {}, "Model versions"), h("p", { class: "sub" }, "Each was recorded from its training manifest after the catalog checked it against Vertex AI and Cloud Storage."),
    table(["Model", "Version", "Trained on", "Artifact", "Recorded"], models.map((m) => [m.model, h("a", { href: `#/model/${enc(m.model)}/${enc(m.version)}` }, m.version),
      h("a", { href: `#/dataset/${enc(m.dataset_version)}` }, m.dataset_version), h("code", {}, m.artifact), [when(m.recorded_at), ` by ${m.recorded_by}`]]),
    "No model version is recorded yet. Import one with odcl import --vertex-manifest."));
}

function runsTable(runs) {
  const keys = (list) => list.map((k, n) => [n ? ", " : "", entryLink(k)]);
  return table(["Run", "System", "State", "Started", "Ended", "Read", "Wrote"], runs.map((r) => [
    h("span", { title: r.run }, /^projects\//.test(r.job) ? r.job.split("/").pop() : r.job), r.system, stateBadge(r.state), when(r.started), when(r.ended), keys(r.inputs), keys(r.outputs)]), "No run is recorded yet.");
}

async function runsPage(params) {
  const system = params.get("system") || "";
  const runs = await api(`/v1/runs?limit=200${system ? `&system=${enc(system)}` : ""}`);
  const select = h("select", { "aria-label": "System", onchange: (e) => { location.hash = `#/runs${e.target.value ? `?system=${e.target.value}` : ""}`; } },
    [["", "every system"], ["dataform", "Dataform"], ["vertex", "Vertex AI"], ["openlineage", "OpenLineage"]].map(([v, t]) => h("option", { value: v, selected: v === system }, t)));
  render(h("h1", {}, "Runs"), h("p", { class: "sub" }, "Builds and training runs read from Dataform and Vertex AI, and runs reported over OpenLineage."),
    h("div", { class: "controls" }, select, h("span", { class: "muted" }, plural(runs.length, "run"))), runsTable(runs));
}

// ---- routing --------------------------------------------------------------------------------------------------
async function route() {
  const [path, query] = (location.hash.slice(1) || "/").split("?");
  const params = new URLSearchParams(query || "");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const pages = {
    "": () => overviewPage(),
    search: () => searchPage(params),
    entry: () => entryPage(parts[1]),
    model: () => modelPage(parts[1], parts[2]),
    dataset: () => datasetPage(parts.slice(1).join("/")),
    models: () => modelsPage(),
    runs: () => runsPage(params),
  };
  const page = pages[parts[0] || ""];
  main.setAttribute("aria-busy", "true");
  try {
    if (!page) throw Object.assign(new Error(`There is no page at ${path}`), { status: 404 });
    await page();
  } catch (error) {
    render(failure(error));
  } finally {
    main.removeAttribute("aria-busy");
  }
}

document.getElementById("search").addEventListener("submit", (event) => {
  event.preventDefault();
  location.hash = `#/search?q=${enc(document.getElementById("q").value.trim())}`;
});
window.addEventListener("hashchange", route);
route();
