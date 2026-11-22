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

// Who is signed in: with single sign-on, the browser's session cookie; otherwise an API token pasted once and kept
// for this tab only (sessionStorage), sent as a bearer token.
const TOKEN = "odcl-token";
async function api(path, { method = "GET", body, retried = false } = {}) {
  const token = sessionStorage.getItem(TOKEN);
  const headers = { Accept: "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}),
                    ...(body !== undefined ? { "Content-Type": "application/json" } : {}) };
  const response = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, credentials: "same-origin" });
  const answer = await response.json().catch(() => ({ error: { message: `${response.status} ${response.statusText}` } }));
  if (response.status === 401 && !retried) {
    if (answer.error?.sign_in && !token) {
      location.href = `${answer.error.sign_in}?next=${enc(location.pathname + location.hash)}`;
      return new Promise(() => {});  // leaving the page
    }
    sessionStorage.removeItem(TOKEN);
    const given = window.prompt(`${answer.error?.message || "Sign in"}\n\nPaste an odcl API token (odcl users token):`);
    if (given && given.trim()) {
      sessionStorage.setItem(TOKEN, given.trim());
      showAccount();
      return api(path, { method, body, retried: true });
    }
  }
  if (!response.ok) throw Object.assign(new Error(answer.error?.message || response.statusText), { code: answer.error?.code, status: response.status });
  return answer;
}

const enc = encodeURIComponent;
const entryHref = (key) => `#/entry/${enc(key)}`;
const short = (key) => String(key).replace(/^(table|column|project|term|domain):/, "");
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

// ---- links to the Google Cloud console ------------------------------------------------------------------------
// Built from the identifiers the catalog already holds; each answers null when the identifier is not one it knows.
const CONSOLE = "https://console.cloud.google.com";
const gcs = (uri) => /^gs:\/\/([^/]+)\/?(.*)$/.exec(uri || "");
const path = (p) => p.split("/").map(enc).join("/");
const gcp = {
  table(name) {  // project.dataset.table; a domain-scoped project keeps its dots and colon
    const parts = String(name || "").replace(/^table:/, "").split(".");
    if (parts.length < 3) return null;
    const t = parts.pop(), d = parts.pop(), p = parts.join(".");
    return `${CONSOLE}/bigquery?project=${enc(p)}&ws=${enc(`!1m5!1m4!4m3!1s${p}!2s${d}!3s${t}`)}`;  // the console's own form; it still accepts p, d and t
  },
  job(project, location, id) {
    return project && id ? `${CONSOLE}/bigquery?project=${enc(project)}&j=${enc(`bq:${location || "US"}:${id}`)}&page=queryresults` : null;
  },
  pipelineRun(name) {
    const m = /^projects\/([^/]+)\/locations\/([^/]+)\/pipelineJobs\/([^/]+)$/.exec(name || "");
    return m ? `${CONSOLE}/vertex-ai/locations/${enc(m[2])}/pipelines/runs/${enc(m[3])}?project=${enc(m[1])}` : null;
  },
  invocation(name) {
    const m = /^projects\/([^/]+)\/locations\/([^/]+)\/repositories\/([^/]+)\/workflowInvocations\/([^/]+)$/.exec(name || "");
    return m ? `${CONSOLE}/bigquery/dataform/locations/${enc(m[2])}/repositories/${enc(m[3])}/workflows/${enc(m[4])}?project=${enc(m[1])}` : null;
  },
  object(uri) {
    const m = gcs(uri);
    return m && m[2] ? `${CONSOLE}/storage/browser/_details/${enc(m[1])}/${path(m[2])}` : null;
  },
  folder(uri) {
    const m = gcs(uri);
    return m ? `${CONSOLE}/storage/browser/${enc(m[1])}/${path(m[2].replace(/\/?$/, "/"))}` : null;
  },
};

function consoleLink(url, text = "Console") {
  return url ? h("a", { class: "console", href: url, target: "_blank", rel: "noopener noreferrer", title: "Open in the Google Cloud console" }, text, " ↗") : null;
}

// A run's state in its own system's words, marked by the outcome the server read it as (odcl/runs.py). A trace
// that leaves the outcome out holds only runs already checked to have succeeded.
const OUTCOME_BADGE = { succeeded: ["good", "✓"], failed: ["critical", "✕"], cancelled: ["critical", "✕"], skipped: ["", "–"], running: ["", "◷"], unknown: ["", "?"] };

function stateBadge(state, outcome = "succeeded") {
  const [cls, mark] = OUTCOME_BADGE[outcome] || OUTCOME_BADGE.unknown;
  return h("span", { class: `status ${cls}`, title: outcome }, `${mark} `, String(state || "UNKNOWN").replace("PIPELINE_STATE_", ""));
}

function changedBadge(severity) {
  const title = "Redefined after this link was recorded" + (severity ? `; the worst change is ${severity}` : "");
  return h("span", { class: `status ${severity === "breaking" ? "critical" : "warning"}`, title }, "△ changed since" + (severity ? ` · ${severity}` : ""));
}

// cosmetic: what people read; additive: new; semantic: the data may mean something else; breaking: what used it may fail
function severityBadge(severity) {
  const cls = { breaking: "status critical", semantic: "status warning" }[severity] || "status";
  return h("span", { class: cls }, severity);
}

async function changesSection(key, since) {
  const box = h("div", {});
  const show = async (from) => {
    const d = await api(`/v1/entries/${enc(key)}/diff${from ? `?since=${enc(from)}` : ""}`);
    box.replaceChildren(
      h("p", { class: "muted" }, d.from ? ["From ", h("code", {}, d.from.revision), " (", when(d.from.at), ") to ", h("code", {}, d.to.revision), " (", when(d.to.at), ")",
        d.severity ? [": the worst change is ", severityBadge(d.severity)] : ": nothing changed"] : "Since it was first registered."),
      d.changes.length ? table(["What", "Change", "Before", "After", ""], d.changes.map((c) => [
        c.key === key ? "this entry" : entryLink(c.key, c.key.split(".").pop()), c.change,
        c.added ? (c.removed.length ? c.removed.map(short).join(", ") : "") : fmt(c.before), c.added ? (c.added.length ? c.added.map(short).join(", ") : "") : fmt(c.after),
        severityBadge(c.severity)])) : null);
  };
  await show(since);
  box.show = show;
  return box;
}

function fmt(value) {
  if (value === null || value === undefined || value === "") return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 80 ? h("span", { title: text }, `${text.slice(0, 80)}…`) : text;
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

// ---- the lineage explorer: every edge, laid out in columns by steps from the focus -----------------------------
async function explorerPage(key, params) {
  const direction = params.get("direction") || "both", depth = params.get("depth") || "", level = params.get("level") || "column";
  const projects = params.get("projects") === "1";
  const go = (next) => {
    const all = { direction, depth, level, projects: projects ? "1" : "", ...next };
    const q = new URLSearchParams();
    if (all.direction !== "both") q.set("direction", all.direction);
    if (all.depth) q.set("depth", all.depth);
    if (all.level !== "column") q.set("level", all.level);
    if (all.projects) q.set("projects", "1");
    location.hash = `#/lineage/${enc(next.key || key)}${q.toString() ? `?${q}` : ""}`;
  };
  const g = await api(`/v1/entries/${enc(key)}/graph?direction=${direction}${depth ? `&depth=${depth}` : ""}&level=${level}${projects ? "&projects=1" : ""}`);
  const focus = g.nodes.find((n) => n.key === g.focus);
  const controls = h("div", { class: "controls" },
    select("Direction", direction, [["both", "both ways"], ["up", "built from"], ["down", "feeds"]], (v) => go({ direction: v })),
    select("Depth", depth, [["", "all steps"], ["1", "1 step"], ["2", "2 steps"], ["3", "3 steps"], ["5", "5 steps"]], (v) => go({ depth: v })),
    select("Show", level, [["column", "columns"], ["table", "tables"]], (v) => go({ level: v })),
    h("label", {}, h("input", { type: "checkbox", checked: projects, onchange: (e) => go({ projects: e.target.checked ? "1" : "" }) }), " projects that use it"),
    h("input", { type: "search", placeholder: "Highlight…", "aria-label": "Highlight entries whose name contains", oninput: (e) => highlight(e.target.value) }));
  if (g.nodes.length === 1) {
    render(h("h1", {}, `Lineage of ${focus.title}`), controls, h("p", { class: "muted" }, "No lineage is recorded for this entry, in this direction and depth."));
    return;
  }

  // columns: upstream to the left of the focus, downstream to the right, projects last
  const dataCols = g.nodes.filter((n) => n.kind !== "project").map((n) => n.side * n.depth);
  const lastCol = Math.max(0, ...dataCols);
  const colOf = (n) => (n.kind === "project" ? lastCol + 1 : n.side * n.depth);
  const byKey = new Map(g.nodes.map((n) => [n.key, n]));
  const columns = new Map();
  for (const n of g.nodes) {
    n.col = colOf(n);
    if (!columns.has(n.col)) columns.set(n.col, []);
    columns.get(n.col).push(n);
  }
  const cols = [...columns.keys()].sort((a, b) => a - b);
  const neighbours = new Map(g.nodes.map((n) => [n.key, []]));
  for (const e of g.edges) {
    neighbours.get(e.from)?.push(e.to);
    neighbours.get(e.to)?.push(e.from);
  }
  // order each column by where its neighbours sit, a few sweeps each way, so edges cross less
  for (const list of columns.values()) list.sort((a, b) => a.title.localeCompare(b.title)).forEach((n, i) => { n.row = i; });
  for (let pass = 0; pass < 6; pass++) {
    const order = pass % 2 ? [...cols].reverse() : cols;
    for (const col of order) {
      const list = columns.get(col);
      const centre = (n) => {
        const near = neighbours.get(n.key).map((k) => byKey.get(k)).filter((m) => m && m.col !== col);
        return near.length ? near.reduce((sum, m) => sum + m.row, 0) / near.length : n.row;
      };
      list.map((n) => [n, centre(n)]).sort((a, b) => a[1] - b[1] || a[0].title.localeCompare(b[0].title)).forEach(([n], i) => { n.row = i; });
    }
  }
  const W = 220, H = 44, GX = 96, GY = 14, PAD = 10;
  const tallest = Math.max(...[...columns.values()].map((l) => l.length));
  const width = cols.length * (W + GX) - GX + PAD * 2, height = tallest * (H + GY) - GY + PAD * 2;
  const x = (n) => PAD + cols.indexOf(n.col) * (W + GX);
  const y = (n) => PAD + ((tallest - columns.get(n.col).length) * (H + GY)) / 2 + n.row * (H + GY);

  const edgeEls = g.edges.filter((e) => byKey.has(e.from) && byKey.has(e.to)).map((e) => {
    const a = byKey.get(e.from), b = byKey.get(e.to);
    const forward = x(a) < x(b);
    const x1 = forward ? x(a) + W : x(a) + W / 2, y1 = forward ? y(a) + H / 2 : y(a) + H;
    const x2 = forward ? x(b) : x(b) + W / 2, y2 = forward ? y(b) + H / 2 : y(b);
    const d = forward ? `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}` : `M${x1},${y1} C${x1},${y1 + 30} ${x2},${y2 - 30} ${x2},${y2}`;
    const el = s("path", { class: `edge ${e.kind}${e.changed_since ? " changed" : ""}`, d });
    el.append(s("title", {}, `${short(e.from)} → ${short(e.to)}${e.kind === "copy" ? " (a copy of its definition)" : e.kind === "uses" ? " (uses)" : ""}${e.changed_since ? "; redefined since" : ""}`));
    return { e, el };
  });
  const panel = h("div", { class: "card explorer-panel" }, h("p", { class: "muted" }, "Hover an entry to follow its lineage; click it for details."));
  const nodeEls = g.nodes.map((n) => {
    const tail = n.kind === "project" ? n.title : n.title.split(".").slice(-2).join(".");
    const label = tail.length > 30 ? `${tail.slice(0, 29)}…` : tail;
    const el = s("g", { class: `node${n.key === g.focus ? " focus" : ""}${n.retired ? " retired" : ""}`, transform: `translate(${x(n)},${y(n)})`, tabindex: 0,
      role: "button", "aria-label": `${n.kind} ${n.title}`,
      onmouseenter: () => trace(n.key), onmouseleave: () => trace(null), onfocus: () => trace(n.key), onblur: () => trace(null),
      onclick: () => choose(n), onkeydown: (ev) => { if (ev.key === "Enter") choose(n); } },
    s("rect", { class: "box", width: W, height: H, rx: 6 }),
    s("rect", { x: 0, y: 0, width: 5, height: H, rx: 2, fill: `var(--${n.kind})` }),
    s("text", { class: "kindlabel", x: 14, y: 17 }, n.kind.toUpperCase() + (n.layer ? ` · ${n.layer}` : "") + (n.retired ? " · RETIRED" : "")),
    s("text", { x: 14, y: 35 }, label));
    el.append(s("title", {}, n.title));
    return { n, el };
  });

  // what feeds the hovered entry, and what it feeds, stays lit; the rest fades
  const into = new Map(), outOf = new Map();
  for (const { e } of edgeEls) {
    (into.get(e.to) || into.set(e.to, []).get(e.to)).push(e.from);
    (outOf.get(e.from) || outOf.set(e.from, []).get(e.from)).push(e.to);
  }
  const reach = (start, next) => {
    const seen = new Set([start]), stack = [start];
    while (stack.length) for (const k of next.get(stack.pop()) || []) if (!seen.has(k)) { seen.add(k); stack.push(k); }
    return seen;
  };
  function trace(k) {
    const lit = k ? new Set([...reach(k, into), ...reach(k, outOf)]) : null;
    for (const { n, el } of nodeEls) el.classList.toggle("dim", Boolean(lit) && !lit.has(n.key));
    for (const { e, el } of edgeEls) {
      const on = lit && lit.has(e.from) && lit.has(e.to);
      el.classList.toggle("dim", Boolean(lit) && !on);
      el.classList.toggle("lit", Boolean(on));
    }
  }
  function highlight(text) {
    const t = text.trim().toLowerCase();
    for (const { n, el } of nodeEls) el.classList.toggle("match", Boolean(t) && n.title.toLowerCase().includes(t));
  }
  function choose(n) {
    for (const { el, n: m } of nodeEls) el.classList.toggle("selected", m === n);
    const ins = (into.get(n.key) || []).length, outs = (outOf.get(n.key) || []).length;
    panel.replaceChildren(
      h("div", {}, kindBadge(n.kind), n.retired ? [" ", retiredBadge()] : null),
      h("h3", {}, n.title), n.description ? h("p", {}, n.description) : h("p", { class: "muted" }, "No description yet."),
      h("p", { class: "muted" }, [n.layer ? `${n.layer} · ` : "", n.system ? `${n.system} · ` : "", n.dtype ? `${n.dtype} · ` : "",
        `${plural(ins, "input")} here, feeds ${plural(outs, "entry", "entries")} here`]),
      h("div", { class: "actions" }, h("a", { href: entryHref(n.key) }, "Open"),
        n.kind !== "project" && n.key !== g.focus ? h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); go({ key: n.key }); } }, "Explore from here") : null));
  }

  let zoom = 1;
  const svg = s("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": `Lineage of ${focus.title}` },
    edgeEls.map((x) => x.el), nodeEls.map((x) => x.el));
  const box = h("div", { class: "card graph explorer" }, svg);
  const scale = (z) => { zoom = Math.min(2, Math.max(0.2, z)); svg.setAttribute("width", width * zoom); svg.setAttribute("height", height * zoom); };
  const ups = g.nodes.filter((n) => n.side < 0).length, downs = g.nodes.filter((n) => n.side > 0 && n.kind !== "project").length;
  render(
    h("h1", {}, ["Lineage of ", entryLink(focus.key, focus.title)]),
    h("p", { class: "sub" }, `${plural(ups, "entry", "entries")} upstream, ${plural(downs, "entry", "entries")} downstream, ${plural(g.edges.length, "edge")}.`
      + (g.truncated ? " Only the nearest are shown; narrow it by depth." : "")),
    controls,
    h("div", { class: "controls" }, h("button", { type: "button", onclick: () => scale(zoom / 1.25) }, "−"), h("button", { type: "button", onclick: () => scale(zoom * 1.25) }, "+"),
      h("button", { type: "button", onclick: () => scale(box.clientWidth / width) }, "Fit"), h("span", { class: "muted" }, "Inputs on the left, what they feed on the right.")),
    h("div", { class: "explorer-layout" }, box, panel),
    h("div", { class: "legend muted" }, kindBadge("table"), kindBadge("column"), kindBadge("project"),
      h("span", {}, "dotted: a copy of its definition"),
      h("span", {}, "dashed amber: redefined after the link was recorded")));
  if (width > box.clientWidth) scale(Math.max(0.75, box.clientWidth / width));  // smaller is hard to read: scroll instead
}

function lineageLists(lineage) {
  const row = (n) => [h("span", {}, "".padStart((n.depth - 1) * 2, " "), entryLink(n.key, n.title)), kindBadge(n.kind), String(n.depth),
    n.changed_since ? changedBadge() : ""];
  return h("div", { class: "two" },
    h("div", {}, h("h2", {}, "Built from"), table(["Entry", "Kind", "Hops", ""], (lineage.upstream || []).map(row), "Nothing upstream.")),
    h("div", {}, h("h2", {}, "Feeds"), table(["Entry", "Kind", "Hops", ""], (lineage.downstream || []).map(row), "Nothing downstream.")));
}

// ---- pages ----------------------------------------------------------------------------------------------------
const retiredBadge = () => h("span", { class: "status warning", title: "No longer at its source" }, "retired");

async function glossaryPage() {
  const terms = await api("/v1/glossary");
  const byDomain = new Map();
  for (const t of terms) byDomain.set(t.domain || "", [...(byDomain.get(t.domain || "") || []), t]);
  render(
    h("h1", {}, "Glossary"),
    h("p", { class: "sub" }, "The business's words for its data, what each means, and which tables and columns mean it."),
    terms.length ? [...byDomain].sort(([a], [b]) => (a || "~").localeCompare(b || "~")).map(([domain, list]) => [
      h("h2", {}, domain ? entryLink(domain) : "No domain"),
      table(["Term", "Definition", "Also called", "Meant by", "Status"], list.map((t) => [entryLink(t.key, t.title), t.definition, t.synonyms.join(", "),
        plural(t.meant_by, "entry", "entries"), t.status || ""]))]) : h("p", { class: "muted" }, "No glossary term yet. Add one with odcl term add."));
}

async function overviewPage() {
  const [overview, models, runs] = await Promise.all([api("/v1/overview"), api("/v1/models"), api("/v1/runs?limit=8")]);
  const tile = (value, label, href) => h(href ? "a" : "div", { class: "card tile", href }, h("div", { class: "value" }, value.toLocaleString()), h("div", { class: "label" }, label));
  render(
    h("h1", {}, "Overview"),
    h("p", { class: "sub" }, "What data exists, how it connects, and which projects and models use it."),
    h("div", { class: "grid" },
      tile(overview.counts.table, "tables", "#/tables"), tile(overview.counts.column, "columns", "#/search?kind=column"),
      tile(overview.counts.project, "projects", "#/projects"), tile(models.length, "model versions", "#/models")),
    h("h2", {}, "Changed after a project used it"),
    table(["Project", "Uses", ""], overview.changed_since_used.map((c) => [entryLink(c.project), entryLink(c.key), changedBadge(c.severity)]),
      "Every project uses data as it is defined now."),
    h("h2", {}, "Gone from its source while a project uses it"),
    table(["Project", "Uses", ""], (overview.retired_but_used || []).map((c) => [entryLink(c.project), entryLink(c.key), retiredBadge()]),
      `Nothing a project uses has been retired.${overview.retired ? ` ${plural(overview.retired, "table or column", "tables and columns")} retired in all.` : ""}`),
    h("h2", {}, "Failing a test while a project uses it"),
    table(["Project", "Uses", "Failing", "Test"], (overview.failing_and_used || []).map((c) => [entryLink(c.project), entryLink(c.key),
      c.failing === c.key ? "itself" : entryLink(c.failing, short(c.failing).split(".").pop()), [c.test, " ", qualityBadge("fail")]]),
      "Nothing a project uses is failing a test, as far as the results recorded say."),
    h("h2", {}, "Redefined after a document described it"),
    table(["Document", "Describes", ""], (overview.changed_since_described || []).map((c) => [entryLink(c.document), entryLink(c.key), changedBadge()]),
      "Every document describes data as it is defined now."),
    h("h2", {}, "Corrected after a model version relied on it"),
    table(["Model version", "Document", ""], (overview.changed_since_relied_on || []).map((c) => [
      h("a", { href: `#/model/${enc(c.model)}/${enc(c.version)}` }, `${c.model} ${c.version}`), entryLink(c.document), changedBadge()]),
      "Every model version relied on its documents as they read now."),
    h("h2", {}, "Recent model versions"),
    table(["Model", "Version", "Trained on", "Recorded"], models.slice(0, 8).map((m) => [entryLink(`project:${m.model}`, m.model),
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
    ["", "table", "column", "project", "document", "term", "domain"].map((k) => h("option", { value: k, selected: k === kind }, k ? `${k}s` : "everything")));
  render(
    h("h1", {}, q ? `Results for “${q}”` : `All ${kind ? `${kind}s` : "entries"}`),
    h("div", { class: "controls" }, h("span", { class: "muted" }, plural(results.length, "result")), select),
    results.length ? h("ul", { class: "results" }, results.map((r) => h("li", {},
      h("div", {}, kindBadge(r.kind), " ", h("a", { class: "title", href: entryHref(r.key) }, r.title),
        r.layer ? h("span", { class: "muted" }, ` · ${r.layer}`) : null, r.dtype ? h("span", { class: "muted" }, ` · ${r.dtype}`) : null,
        r.used_by ? h("span", { class: "muted" }, ` · used by ${plural(r.used_by, "project")}`) : null, r.retired ? [" ", retiredBadge()] : null),
      r.description ? h("p", {}, r.description) : null))) : h("p", { class: "muted" }, "Nothing matches every word."));
}

async function entryPage(key) {
  const entry = await api(`/v1/entries/${enc(key)}`);
  if (entry.kind === "document") return documentPage(entry);
  const isModel = entry.kind === "project" && entry.model_versions.length > 0;
  const parts = [
    h("div", {}, kindBadge(entry.kind), isModel ? [" ", h("span", { class: "badge" }, "model")] : null, entry.retired ? [" ", retiredBadge()] : null),
    h("h1", {}, entry.title),
    entry.retired ? h("div", { class: "missing" }, `△ No longer at its source: ${entry.retired_by || "its importer"} did not find it at its last import. What it was is kept in its history.`) : null,
    entry.description ? h("p", { class: "sub" }, entry.description) : h("p", { class: "sub muted" }, "No description yet."),
    h("div", { class: "card" }, facts([
      ["Key", h("code", {}, entry.key)],
      ["Table", entry.table ? entryLink(entry.table) : null],
      ["Type", entry.dtype], ["Definition", entry.definition ? h("code", {}, entry.definition) : null],
      ["Layer", entry.layer], ["System", entry.system], ["Owner", entry.owner], ["Status", entry.status],
      ["Repository", entry.repository],
      ["Tags", (entry.tags || []).join(", ")],
      ["Created by", createdBy(entry.created_by)],
      ["References", (entry.references || []).length ? entry.references.map((d, n) => [n ? ", " : "", entryLink(d.key, d.title), d.changed_since ? [" ", changedBadge()] : ""]) : null],
      ["Foreign keys", (entry.foreign_keys || []).length ? entry.foreign_keys.map((f, n) => [n ? "; " : "", entryLink(f.column, f.column.split(".").pop()), " → ", entryLink(f.references, f.references.replace(/^column:/, ""))]) : null],
      ["Referenced by", (entry.referenced_by || []).length ? entry.referenced_by.map((d, n) => [n ? ", " : "", entryLink(d.key || d.column, (d.title || d.column).replace(/^column:/, ""))]) : null],
      ["Copy of", (entry.copy_of || []).length ? entry.copy_of.map((d, n) => [n ? ", " : "", entryLink(d.key, d.title)]) : null],
      ["Copies", (entry.instances || []).length ? [entry.instances.map((p, n) => [n ? ", " : "", h("code", {}, p)]),
        ` match ${(entry.copies || []).length}: `, (entry.copies || []).map((d, n) => [n ? ", " : "", entryLink(d.key, d.title)])] : null],
      ["Also called", (entry.synonyms || []).join(", ") || null],
      ["Means", (entry.terms || []).length ? entry.terms.map((t, n) => [n ? ", " : "", entryLink(t.key, t.title)]) : null],
      ["Domain", (entry.domain || []).length ? entry.domain.map((d) => entryLink(d.key, d.title)) : null],
      ["Personal data", entry.pii === true ? "yes" : entry.pii === false ? "no" : null], ["Scrubbing", entry.scrubbing],
      ["Data category", entry.data_category], ["Retention", entry.retention],
      ["Revision", [`${entry.revision} by ${entry.registered_by}, `, when(entry.at)]],
      ["Sources", sources(entry.source)],
    ])),
  ];
  if (entry.kind === "term") {
    parts.push(h("h2", {}, `Meant by (${entry.meant_by.length})`),
      table(["Entry", "Kind", "Description"], entry.meant_by.map((m) => [entryLink(m.key, m.title), kindBadge(m.kind), m.description || ""]),
        "No table or column says it means this yet."));
  } else if (entry.kind === "domain") {
    parts.push(h("h2", {}, `In this domain (${entry.members.length})`),
      table(["Entry", "Kind", "Description"], entry.members.map((m) => [entryLink(m.key, m.title), kindBadge(m.kind), m.description || ""]),
        "Nothing is in this domain yet."));
  } else if (entry.kind === "project") {
    if (isModel) {
      parts.push(h("h2", {}, `Model versions (${entry.model_versions.length})`), modelVersionsTable(entry.model_versions, false));
    }
    parts.push(h("h2", {}, "Uses"),
      isModel ? h("p", { class: "muted" }, "What its newest model version was trained on is recorded here at import, pinned to the definitions that version used.") : null,
      table(["Entry", "Kind", "For", ""], entry.uses.map((u) => [entryLink(u.key, u.title), kindBadge(u.kind), u.purpose || "", u.changed_since ? changedBadge(u.severity) : ""]),
        "This project has recorded no use of data yet."));
  } else {
    const lineage = await api(`/v1/entries/${enc(entry.key)}/lineage?direction=both`);
    if (lineage.system === "bigquery") {
      parts[4].append(h("div", { class: "console-row" }, consoleLink(gcp.table(entry.kind === "column" ? entry.table : entry.key), "Open in BigQuery")));
    }
    parts.push(h("h2", {}, ["Lineage ", h("a", { class: "aside", href: `#/lineage/${enc(entry.key)}` }, "Explore →")]), lineageGraph(lineage), lineageLists(lineage));
    if (entry.kind === "table") {
      parts.push(h("h2", {}, `Columns (${entry.columns.length})`),
        table(["Column", "Type", "Description"], entry.columns.map((c) => [entryLink(c.key, c.title.split(".").pop()), c.dtype || "", c.description || ""]),
          "No columns are registered."));
      const proposals = (await api(`/v1/privacy/proposals?table=${enc(entry.key)}&min_confidence=medium`).catch(() => ({}))).proposals || [];
      if (proposals.length) {
        parts.push(h("details", {}, h("summary", { class: "muted" }, `${plural(proposals.length, "column")} may hold personal data and say nothing about it yet`),
          h("p", { class: "muted" }, "Proposed from their names, glossary terms and descriptions, not their values. Review with odcl privacy propose --write FILE."),
          table(["Column", "Category", "Why", "Confidence"], proposals.map((p) => [entryLink(p.key, p.key.split(".").pop()), p.data_category, p.reason, p.confidence]))));
      }
      if ((entry.retired_columns || []).length) {
        parts.push(h("details", {}, h("summary", { class: "muted" }, `${plural(entry.retired_columns.length, "column")} no longer at the source`),
          table(["Column", "Type", "Description"], entry.retired_columns.map((c) => [entryLink(c.key, c.title.split(".").pop()), c.dtype || "", c.description || ""]))));
      }
    }
    parts.push(h("h2", {}, "Used by"),
      table(["Project", "How", "Through", "For", ""], entry.used_by.map((u) => [entryLink(u.project), u.how, u.through === entry.key ? "this entry" : entryLink(u.through),
        u.purpose || "", u.changed_since ? changedBadge() : ""]), "No project depends on this."));
    if (entry.kind === "table" && entry.usage) {
      const u = entry.usage;
      parts.push(h("h2", {}, "Usage"), h("p", {}, u.reads ? [`Read by ${plural(u.reads, "query job")}${u.shapes ? ` through ${plural(u.shapes, "query shape")}` : ""} in the last ${u.window_days} days, last `, when(u.last_read), "."]
          : h("span", { class: "muted" }, `No synced query job read it in the last ${u.window_days} days.`)),
        u.read_with.length ? [h("h3", {}, "Often read with"), table(["Table", "Jobs that read both"], u.read_with.map((r) => [r.key ? entryLink(r.key, r.table) : r.table, String(r.jobs)]))] : null);
    }
    if (entry.kind === "table") parts.push(await historySection(entry.key));
  }
  parts.push(documentedIn(entry));
  if ((entry.quality?.results || []).length) {
    parts.push(h("h2", {}, ["Quality ", qualityBadge(entry.quality.status)]), qualityTable(entry.quality.results, entry.key));
  }
  if (entry.kind === "table" || entry.kind === "column") {
    const changes = await changesSection(entry.key);
    parts.push(h("h2", {}, "What changed"), changes,
      h("h2", {}, "History"), table(["Revision", "By", "When", ""], entry.history.map((r, n) => [h("code", {}, r.revision), r.registered_by, when(r.at),
        n ? h("button", { type: "button", class: "link", onclick: () => changes.show(r.revision) }, "changes since") : ""])));
  } else {
    parts.push(h("h2", {}, "History"), table(["Revision", "By", "When"], entry.history.map((r) => [h("code", {}, r.revision), r.registered_by, when(r.at)])));
  }
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
      table(["Job", "Read or write", "State", "Created"], history.jobs.map((j) => [[h("code", {}, j.job_id), " ", consoleLink(gcp.job(j.project, j.location, j.job_id))], j.roles.join(", "), stateBadge(j.state, j.outcome), when(j.created)])),
      history.more ? h("p", { class: "muted" }, "More jobs than shown.") : null);
  } catch {
    return h("div", {});
  }
}

// ---- documents ------------------------------------------------------------------------------------------------
function documentedIn(entry) {
  const docs = entry.documents || [];
  return h("div", {}, h("h2", {}, `Documented in (${docs.length})`),
    docs.length ? h("ul", { class: "results" }, docs.map((d) => h("li", {},
      h("div", {}, kindBadge("document"), " ", entryLink(d.document, d.title || short(d.document)), d.doc_kind ? h("span", { class: "muted" }, ` · ${d.doc_kind}`) : null,
        d.section ? h("span", { class: "muted" }, " · ", h("code", {}, `#${d.section}`)) : null,
        d.through !== entry.key ? h("span", { class: "muted" }, " · about its table ", entryLink(d.through)) : null,
        d.changed_since ? [" ", h("span", { class: "status warning", title: "Redefined after the page described it" }, "△ redefined since the page described it")] : null),
      d.says ? h("p", {}, d.says) : null)))
      : h("p", { class: "muted" }, "No document describes this yet."));
}

// Markdown shown as text: headings, code blocks and paragraphs become elements, but nothing in the file is parsed as HTML
function markdownAsText(source) {
  const out = [];
  let paragraph = [], code = null;
  const flush = () => { if (paragraph.length) out.push(h("p", {}, paragraph.join(" "))); paragraph = []; };
  for (const line of source.split("\n")) {
    if (/^\s{0,3}(```|~~~)/.test(line)) {
      if (code === null) { flush(); code = []; } else { out.push(h("pre", { class: "card mono" }, code.join("\n"))); code = null; }
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    const heading = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) { flush(); out.push(h(`h${Math.min(heading[1].length + 2, 6)}`, {}, heading[2])); continue; }
    if (!line.trim()) { flush(); continue; }
    if (/^\s*\|/.test(line)) { flush(); out.push(h("pre", { class: "mono table-text" }, line)); continue; }
    paragraph.push(line.trim());
  }
  flush();
  if (code !== null) out.push(h("pre", { class: "card mono" }, code.join("\n")));
  return out;
}

function documentPage(d) {
  const safeUrl = /^https?:\/\//.test(d.url || "") ? d.url : null;
  const bySection = new Map();
  for (const link of d.describes) {
    const section = link.section || "";
    if (!bySection.has(section)) bySection.set(section, []);
    bySection.get(section).push(link);
  }
  const unresolved = d.unresolved || [];
  render(
    h("div", {}, kindBadge("document"), d.doc_kind ? [" ", h("span", { class: "badge" }, d.doc_kind)] : null, d.retired ? [" ", h("span", { class: "status warning" }, "retired")] : null),
    h("h1", {}, d.title),
    d.description ? h("p", { class: "sub" }, d.description) : null,
    h("div", { class: "card" }, facts([
      ["Key", h("code", {}, d.key)], ["Owner", d.owner], ["Kind", d.doc_kind],
      ["File", safeUrl ? h("a", { href: safeUrl, target: "_blank", rel: "noopener noreferrer" }, d.path, " ↗") : d.path],
      ["Commit", d.commit ? h("code", {}, d.commit) : "not recorded"],
      ["Patterns", (d.patterns || []).length ? d.patterns.map((p, n) => [n ? ", " : "", h("code", {}, p)]) : null],
      ["Revision", [`${d.revision} by ${d.registered_by}, `, when(d.at)]],
    ])),
    d.retired ? h("div", { class: "missing" }, "△ This page is gone from its repository. What it described is kept in its history, not as current.") : null,
    h("h2", {}, `Describes (${d.describes.length})`),
    d.describes.length ? [...bySection.entries()].map(([section, links]) => h("div", {},
      h("h3", {}, section ? h("code", {}, `#${section}`) : "The whole page"),
      table(["Entry", "Kind", "Says", ""], links.map((l) => [entryLink(l.key, l.title), kindBadge(l.kind), l.says || "", l.changed_since ? changedBadge() : ""]))))
      : h("p", { class: "muted" }, d.retired ? "Nothing now." : "Nothing in the catalog yet."),
    unresolved.length ? [h("h2", {}, `Not found (${unresolved.length})`),
      unresolved.map((u) => h("div", { class: "missing" }, "△ ", u.startsWith("section ") ? `names a ${u} that the page has no heading for` : `names ${u}, which the catalog does not have`))] : null,
    d.headings.length ? [h("h2", {}, "Headings"), h("ul", {}, d.headings.map((x) => h("li", { style: null }, "  ".repeat(x.level - 1), x.text, " ", h("code", { class: "muted" }, `#${x.anchor}`))))] : null,
    (d.relied_on_by || []).length ? [h("h2", {}, `Relied on by (${d.relied_on_by.length})`),
      table(["Model version", "Revision", "For", ""], d.relied_on_by.map((r) => [h("a", { href: `#/model/${enc(r.model)}/${enc(r.version)}` }, `${r.model} ${r.version}`),
        h("code", {}, r.revision), r.why || "", r.changed_since ? h("span", { class: "status warning", title: "The page was corrected after this version relied on it" }, "△ corrected since") : ""]))] : null,
    h("h2", {}, "Page"), h("div", { class: "card markdown" }, markdownAsText(d.text || "")),
    h("h2", {}, "History"), table(["Revision", "By", "When"], d.history.map((r) => [h("code", {}, r.revision), r.registered_by, when(r.at)])));
}

function step(label, what, ...details) {
  return h("li", {}, h("div", { class: "step" }, label), h("div", { class: "what" }, what), details.filter(Boolean).map((d) => h("div", { class: "muted" }, d)));
}

function failingAtTraining(trace) {
  const failing = trace.quality_at_training || [];
  return failing.length ? [h("h2", {}, `Failing a test when it was trained (${failing.length})`),
    qualityTable(failing)] : [];
}

function qualityBadge(status) {
  const cls = { fail: "status critical", error: "status critical", warn: "status warning", pass: "status good" }[status] || "status";
  return h("span", { class: cls }, status);
}

function qualityTable(results, key) {
  return table(["Test", "On", "Status", "Failing", "When", "Source"], results.map((r) => [
    r.kind && r.kind !== r.test ? [r.test, h("span", { class: "muted" }, ` · ${r.kind}`)] : r.test,
    r.entry === key ? "this entry" : entryLink(r.entry, short(r.entry).split(".").pop()), qualityBadge(r.status),
    r.failures ? r.failures.toLocaleString() : "", [when(r.observed_at), r.current === false ? h("div", { class: "muted" }, "before its latest revision") : null],
    r.source]));
}

function reliedOn(trace) {
  return (trace.relied_on || []).length ? [h("h2", {}, `Relied on (${trace.relied_on.length})`),
    table(["Document", "Revision", "For", ""], trace.relied_on.map((d) => [entryLink(d.document, d.title), h("code", {}, d.revision), d.why || "",
      d.changed_since ? h("span", { class: "status warning", title: "The page was corrected after this version relied on it" }, "△ corrected since") : ""]))] : [];
}

function missingList(missing) {
  return [h("h2", {}, missing.length ? `Not established (${missing.length})` : "Not established"),
    missing.length ? missing.map((m) => h("div", { class: "missing" }, "△ ", m)) : h("p", { class: "status good" }, "✓ Every link of this trace is recorded.")];
}

// what a person said created a table or file: a script, a notebook, a query; links only to http(s) addresses
function createdBy(c) {
  if (!c) return null;
  const where = c.path || c.url || c.command || "";
  const safe = /^https?:\/\//.test(c.url || "") ? c.url : null;
  return h("span", {}, h("span", { class: "badge" }, c.kind), " ", safe ? h("a", { href: safe, target: "_blank", rel: "noopener noreferrer" }, where, " ↗") : h("code", {}, where),
    c.commit ? h("span", { class: "muted" }, ` at ${c.commit.slice(0, 12)}`) : null,
    c.uncommitted ? h("span", { class: "status warning", title: "The file had changes not in that commit" }, " △ uncommitted changes") : null,
    c.command && c.command !== where ? h("div", {}, h("code", {}, c.command)) : null, c.note ? h("div", { class: "muted" }, c.note) : null,
    h("div", { class: "muted" }, "as stated; not checked against a run"));
}

function filesSteps(files) {
  const written = files.produced_by;
  const exported = files.exported_from;
  return [
    step("Written by", written ? `${written.pipeline_run.split("/").pop()} / task ${written.task}` : files.created_by ? files.created_by.kind : "not recorded",
      written ? h("span", {}, written.pipeline_run, " ", consoleLink(gcp.pipelineRun(written.pipeline_run)))
        : files.created_by ? createdBy(files.created_by) : "the files were named by URI, not read from a pipeline task's output",
      written?.state ? h("span", {}, stateBadge(written.state, written.outcome)) : null),
    step("Exported from", exported ? entryLink(`table:${exported.table}`, exported.table) : "not recorded",
      exported ? h("span", {}, "table-level lineage only, pinned when the files were recorded",
        exported.changed_since ? [" ", changedBadge()] : null) : null),
    ...(files.exported_by || []).map((j) => {
      const [, project, location, id] = /^(.+):([A-Za-z0-9-]+)\.([\w-]+)$/.exec(j.job) || [];
      return step("Exported by", j.job, h("span", {}, `BigQuery ${j.statement_type} job`, j.rows !== null ? ` · ${plural(j.rows, "row")}` : "",
        j.files !== null ? ` · ${plural(j.files, "file")}` : "", " · ", when(j.created), " ", consoleLink(gcp.job(project, location, id))));
    }),
    ...(files.row_filter ? [step("Row filter", h("code", {}, files.row_filter.expression || "?"),
      files.row_filter.columns.length ? h("span", {}, "filters on ", files.row_filter.columns.map((c, n) => [n ? ", " : "", entryLink(`column:${c}`, c)])) : null)] : [])];
}

function fieldSources(c) {
  return (c.derived_from || []).map((d, n) => [n ? ", " : "", entryLink(`column:${d.column ?? d}`, d.column ?? d), d.changed_since ? [" ", changedBadge()] : ""]);
}

function lineageCell(status) {
  return status === "resolved" ? h("span", { class: "status good" }, "✓ resolved")
    : status === "not_claimed" ? h("span", { class: "muted" }, "not claimed") : h("span", { class: "status warning" }, `△ ${status}`);
}

async function modelPage(name, version) {
  const trace = await api(`/v1/models/${enc(name)}/versions/${enc(version)}/trace`);
  const p = trace.produced_by, read = trace.read;
  if (read.kind === "gcs_files") return modelOnFilesPage(trace);
  render(
    h("div", {}, kindBadge("project"), h("span", { class: "muted" }, " model version")),
    h("h1", {}, `${trace.model} ${trace.version}`),
    h("p", { class: "sub" }, "From the training task back to the raw column behind each input, as pinned when the model was trained."),
    h("ol", { class: "trace" },
      step("Model artifact", trace.artifact.uri, `generation ${trace.artifact.generation}`, `sha256 ${trace.artifact.sha256}`, consoleLink(gcp.object(trace.artifact.uri))),
      step("Trained by", `${p.pipeline_run.split("/").pop()} / task ${p.task}`, h("span", {}, p.pipeline_run, " ", consoleLink(gcp.pipelineRun(p.pipeline_run))),
        p.training_commit ? `training code at commit ${p.training_commit}` : "training code commit not recorded",
        p.prediction_cutoff ? `prediction cutoff ${p.prediction_cutoff}` : null),
      step("Read dataset version", h("a", { href: `#/dataset/${enc(read.dataset_version)}` }, read.dataset_version),
        h("span", {}, "a snapshot of ", entryLink(`table:${read.snapshot_of}`), " ", consoleLink(gcp.table(read.snapshot), "Snapshot in BigQuery")))),
    h("h2", {}, `Inputs (${trace.inputs.length})`),
    trace.inputs.map((i) => h("div", { class: "card", style: null },
      h("div", {}, h("strong", {}, i.field), h("span", { class: "muted" }, ` · ${i.role}${i.position !== null ? ` #${i.position}` : ""}`),
        i.lineage !== "resolved" ? h("span", { class: "status warning" }, ` △ lineage ${i.lineage}`) : null),
      h("ol", { class: "trace", style: null },
        step("Expression", h("code", {}, i.expression ?? "?"), i.column ? h("span", {}, "column ", entryLink(`column:${i.column}`)) : null),
        step("Built by", i.produced_by.dataform_invocation ? i.produced_by.dataform_invocation.split("/").pop() : "?",
          i.produced_by.dataform_invocation, i.produced_by.state ? h("span", {}, stateBadge(i.produced_by.state), " ", consoleLink(gcp.invocation(i.produced_by.dataform_invocation))) : null),
        step("Executed as", i.executed_as ? i.executed_as.job_id : "an unknown job",
          i.executed_as ? h("span", {}, `BigQuery job in ${i.executed_as.project}, ${i.executed_as.location} · ${i.executed_as.statement_type} `,
            consoleLink(gcp.job(i.executed_as.project, i.executed_as.location, i.executed_as.job_id))) : null),
        step("Defined in", i.defined_in.file || "?", i.defined_in.commit ? `commit ${i.defined_in.commit}` : "no git commit"),
        step("From raw columns", i.derived_from.length ? i.derived_from.map((c, n) => [n ? ", " : "", entryLink(`column:${c}`, c)]) : "?")))),
    ...reliedOn(trace), ...failingAtTraining(trace), ...missingList(trace.missing));
}

function modelOnFilesPage(trace) {
  const p = trace.produced_by, read = trace.read;
  render(
    h("div", {}, kindBadge("project"), h("span", { class: "muted" }, " model version")),
    h("h1", {}, `${trace.model} ${trace.version}`),
    h("p", { class: "sub" }, read.exported_by
      ? "Trained on files a BigQuery export job wrote. The files prove the exact bytes the trainer read; the job's SELECT is each field's definition."
      : "Trained on files a pipeline exported, not on a BigQuery snapshot. The files prove the exact bytes the trainer read; "
        + "they do not show which build produced them, so no column lineage is claimed below them."),
    h("ol", { class: "trace" },
      step("Model artifact", trace.artifact.uri, `generation ${trace.artifact.generation}`, `sha256 ${trace.artifact.sha256}`, consoleLink(gcp.object(trace.artifact.uri))),
      step("Trained by", `${p.pipeline_run.split("/").pop()} / task ${p.task}`, h("span", {}, p.pipeline_run, " ", consoleLink(gcp.pipelineRun(p.pipeline_run))),
        p.training_commit ? `training code at commit ${p.training_commit}` : "training code commit not recorded"),
      step("Read files", h("a", { href: `#/dataset/${enc(read.dataset_version)}` }, read.uri), `${plural(read.objects, "object")}, generations pinned`,
        consoleLink(gcp.folder(read.uri))),
      ...filesSteps(read)),
    h("h2", {}, `Inputs (${trace.inputs.length})`),
    table(["Field", "Role", "Type", "Expression", "From", "Lineage"], trace.inputs.map((i) => [h("strong", {}, i.field),
      `${i.role}${i.position !== null ? ` #${i.position}` : ""}`, i.dtype || "?", i.expression ? h("code", {}, i.expression) : "", fieldSources(i),
      lineageCell(i.lineage)])),
    ...reliedOn(trace), ...failingAtTraining(trace), ...missingList(trace.missing));
}

function filesPage(d) {
  render(
    h("div", {}, h("span", { class: "kind table" }, "dataset version"), " ", h("span", { class: "badge" }, "files")),
    h("h1", {}, d.reference),
    h("p", { class: "sub" }, "Cloud Storage files a training run read, each pinned to the object generation recorded. " + (d.exported_by
      ? "They are bound to the BigQuery export job that wrote them, whose SELECT defines each field."
      : "Its fields come from the Parquet schema; column lineage below them is not claimed.")),
    h("ol", { class: "trace" }, step("Files", d.uri, `${plural(d.objects.length, "object")}, generations pinned`, consoleLink(gcp.folder(d.uri))), ...filesSteps(d)),
    h("h2", {}, `Fields (${d.columns.length})`),
    table(["Field", "Type", "Expression", "From", "Lineage"], d.columns.map((c) => [c.field, c.dtype || "?", c.expression ? h("code", {}, c.expression) : "",
      fieldSources(c), lineageCell(c.lineage)]), "The fields of these files are not known."),
    h("h2", {}, `Objects (${d.objects.length})`),
    table(["Object", "Generation", "Size"], d.objects.map((o) => [[h("code", {}, o.uri), " ", consoleLink(gcp.object(o.uri))], h("code", {}, o.generation),
      o.size === null ? "?" : `${o.size.toLocaleString()} bytes`])),
    h("h2", {}, "Recorded"), facts([["By", d.recorded_by], ["When", when(d.recorded_at)]]),
    ...missingList(d.missing));
}

async function datasetPage(reference) {
  const d = await api(`/v1/datasets/${enc(reference)}/trace`);
  if (d.kind === "gcs_files") return filesPage(d);
  render(
    h("div", {}, h("span", { class: "kind table" }, "dataset version")),
    h("h1", {}, d.reference),
    h("p", { class: "sub" }, "A snapshot handed on by a build, with each field pinned to the definition that build used."),
    h("ol", { class: "trace" },
      step("Snapshot of", entryLink(`table:${d.snapshot_of}`), `taken ${d.snapshot_time}`, h("span", {}, consoleLink(gcp.table(d.snapshot), "Snapshot in BigQuery"))),
      step("Produced by", d.produced_by.dataform_invocation ? d.produced_by.dataform_invocation.split("/").pop() : "?", d.produced_by.dataform_invocation,
        h("span", {}, stateBadge(d.produced_by.state, d.produced_by.outcome), " ", when(d.produced_by.started), " → ", when(d.produced_by.ended), " ",
          consoleLink(gcp.invocation(d.produced_by.dataform_invocation)))),
      step("Executed as", d.executed_as ? d.executed_as.job_id : "an unknown job",
        d.executed_as ? h("span", {}, `BigQuery job in ${d.executed_as.project}, ${d.executed_as.location} · ${d.executed_as.statement_type} · ${(d.executed_as.billed_bytes ?? 0).toLocaleString()} bytes billed `,
          consoleLink(gcp.job(d.executed_as.project, d.executed_as.location, d.executed_as.job_id))) : null),
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

function modelVersionsTable(models, withModel = true) {
  return table([...(withModel ? ["Model"] : []), "Version", "Trained on", "Artifact", "Recorded"], models.map((m) => [
    ...(withModel ? [entryLink(`project:${m.model}`, m.model)] : []),
    h("a", { href: `#/model/${enc(m.model)}/${enc(m.version)}` }, m.version),
    h("a", { href: `#/dataset/${enc(m.dataset_version)}` }, m.dataset_version), [h("code", {}, m.artifact), " ", consoleLink(gcp.object(m.artifact))],
    [when(m.recorded_at), ` by ${m.recorded_by}`]]),
  "No model version is recorded yet. Import one with odcl import --vertex-manifest.");
}

async function modelsPage() {
  const models = await api("/v1/models");
  render(h("h1", {}, "Model versions"),
    h("p", { class: "sub" }, "Each trained version of a model, recorded from its training manifest after the catalog checked it against Vertex AI and Cloud Storage. A model is a project; its page lists its versions."),
    modelVersionsTable(models));
}

function select(label, value, options, go) {
  return h("label", { class: "muted" }, `${label} `, h("select", { "aria-label": label, onchange: (e) => go(e.target.value) },
    options.map(([v, t]) => h("option", { value: v, selected: v === value }, t))));
}

async function tablesPage(params) {
  const layer = params.get("layer") || "", system = params.get("system") || "", sort = params.get("sort") || "";
  const all = await api(`/v1/tables${sort ? `?sort=${enc(sort)}` : ""}`);
  const shown = all.filter((t) => (!layer || t.layer === layer) && (!system || t.system === system));
  const choices = (field) => [["", "all"], ...[...new Set(all.map((t) => t[field]).filter(Boolean))].sort().map((v) => [v, v])];
  const go = (next) => {
    const q = new URLSearchParams({ layer, system, sort, ...next });
    for (const [k, v] of [...q]) if (!v) q.delete(k);
    location.hash = `#/tables${q.toString() ? `?${q}` : ""}`;
  };
  render(h("h1", {}, "Tables"),
    h("p", { class: "sub" }, "Every table the importers and people have registered. Find a column by searching, or from its table."),
    h("div", { class: "controls" }, select("Layer", layer, choices("layer"), (v) => go({ layer: v })),
      select("System", system, choices("system"), (v) => go({ system: v })),
      select("Order", sort, [["", "by name"], ["reads", "most read first"]], (v) => go({ sort: v })), h("span", { class: "muted" }, plural(shown.length, "table"))),
    shown.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Table", "Layer", "System", "Columns", "Used by", "Reads (30d)", "Changed"].map((x, i) => h("th", { scope: "col", class: i >= 3 && i <= 5 ? "num" : null }, x)))),
      h("tbody", {}, shown.map((t) => h("tr", {},
        h("td", {}, entryLink(t.key, t.title), t.description ? h("div", { class: "muted" }, t.description.length > 140 ? `${t.description.slice(0, 140)}…` : t.description) : null),
        h("td", {}, t.layer || ""), h("td", {}, t.system || ""), h("td", { class: "num" }, String(t.columns)),
        h("td", { class: "num" }, t.used_by ? plural(t.used_by, "project") : "—"), h("td", { class: "num" }, t.reads ? t.reads.toLocaleString() : "—"),
        h("td", {}, when(t.changed))))))
      : h("p", { class: "muted" }, all.length ? "No table matches these filters." : "No table is registered yet."));
}

async function projectsPage() {
  const projects = await api("/v1/projects");
  render(h("h1", {}, "Projects"),
    h("p", { class: "sub" }, "Anything that uses data: a model, an analysis, a dashboard. A project with trained versions is a model."),
    h("div", { class: "controls" }, h("span", { class: "muted" }, plural(projects.length, "project"))),
    projects.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Project", "Owner", "Uses", "Latest model version", "Changed"].map((x, i) => h("th", { scope: "col", class: i === 2 ? "num" : null }, x)))),
      h("tbody", {}, projects.map((p) => h("tr", {},
        h("td", {}, entryLink(p.key, p.title), p.model_versions ? [" ", h("span", { class: "badge" }, "model")] : null,
          p.description ? h("div", { class: "muted" }, p.description) : null),
        h("td", {}, p.owner || ""),
        h("td", { class: "num" }, plural(p.uses, "entry", "entries"), p.changed_since_used ? h("div", {}, h("span", { class: "status warning", title: "Redefined after the project used it" }, `△ ${p.changed_since_used} changed since`)) : null),
        h("td", {}, p.latest_version ? [h("a", { href: `#/model/${enc(p.title)}/${enc(p.latest_version)}` }, p.latest_version),
          p.model_versions > 1 ? h("span", { class: "muted" }, ` of ${p.model_versions}`) : null] : h("span", { class: "muted" }, "—")),
        h("td", {}, when(p.changed))))))
      : h("p", { class: "muted" }, "No project is registered yet. Register one with odcl add project, or import a model version."));
}

// where a run can be opened: its pipeline run (a Vertex task opens its run), its Dataform invocation, or its BigQuery job
function runConsole(r) {
  if (r.system === "vertex") return gcp.pipelineRun(r.kind === "pipeline_run" ? r.run : r.parent);
  if (r.system === "dataform") return gcp.invocation(r.kind === "invocation" ? r.run : r.parent)
    || (r.bigquery_job && gcp.job(r.bigquery_job.project, r.bigquery_job.location, r.bigquery_job.job_id));
  return null;
}

function runsTable(runs) {
  const keys = (list) => list.map((k, n) => [n ? ", " : "", entryLink(k)]);
  return table(["Run", "System", "State", "Started", "Ended", "Read", "Wrote"], runs.map((r) => [
    [h("span", { title: r.run }, /^projects\//.test(r.job) ? r.job.split("/").pop() : r.job), " ", consoleLink(runConsole(r))],
    r.system, stateBadge(r.state, r.outcome), when(r.started), when(r.ended), keys(r.inputs), keys(r.outputs)]), "No run is recorded yet.");
}

async function runsPage(params) {
  const system = params.get("system") || "";
  const runs = await api(`/v1/runs?limit=200${system ? `&system=${enc(system)}` : ""}`);
  const select = h("select", { "aria-label": "System", onchange: (e) => { location.hash = `#/runs${e.target.value ? `?system=${e.target.value}` : ""}`; } },
    [["", "every system"], ["dataform", "Dataform"], ["vertex", "Vertex AI"], ["openlineage", "OpenLineage"]].map(([v, t]) => h("option", { value: v, selected: v === system }, t)));
  render(h("h1", {}, "Runs"), h("p", { class: "sub" }, "Builds and training runs read from Dataform and Vertex AI, and runs reported over OpenLineage."),
    h("div", { class: "controls" }, select, h("span", { class: "muted" }, plural(runs.length, "run"))), runsTable(runs));
}

// ---- who is signed in -----------------------------------------------------------------------------------------
async function showAccount() {
  const slot = document.getElementById("account");
  let me = null;
  try { me = (await api("/v1/me")).user; } catch { /* the page says why where it matters */ }
  slot.replaceChildren(me ? h("a", { href: "#/account", title: `${me.name}, ${me.role}` }, me.display_name || me.name) : "");
}

async function signOut() {
  sessionStorage.removeItem(TOKEN);
  const answer = await api("/auth/logout", { method: "POST" }).catch(() => ({ next: "/" }));
  location.href = answer.next || "/";
}

// whether a subscription's messages get through: failing until one does, with the last error and the next try
function deliveryBadge(delivery) {
  if (!delivery || delivery.status === "ok") return h("span", { class: "status good" }, "✓ ok");
  const next = delivery.next_attempt ? `next try ${delivery.next_attempt}` : "tried again on the next run";
  return h("span", { class: "status critical", title: `${delivery.last_error || "failed"}\nfailing since ${delivery.since} · ${delivery.attempts} tries · ${next}` },
    `✕ failing · ${delivery.attempts} ${delivery.attempts === 1 ? "try" : "tries"}`);
}

// alert subscriptions: an editor's own, or everyone's for an admin; sent by odcl alerts send after imports
async function alertsSection(me) {
  const subscriptions = await api("/v1/alerts/subscriptions");
  const message = h("p", { class: "muted", role: "status" });
  const remove = async (id) => {
    try { await api(`/v1/alerts/subscriptions/${enc(id)}/remove`, { method: "POST" }); route(); } catch (error) { message.textContent = error.message; }
  };
  const field = (label, control) => h("label", { class: "field" }, h("span", {}, label), control);
  const scope = h("select", { "aria-label": "About" }, [["project", "one project"], ["owner", "the projects I own"], ["all", "every project"]].map(([v, t]) =>
    h("option", { value: v }, t)));
  const project = h("input", { type: "text", placeholder: "project name", "aria-label": "Project" });
  const channel = h("select", { "aria-label": "Send to" }, [["slack", "Slack incoming webhook"], ["email", "email"], ["webhook", "webhook (JSON)"]].map(([v, t]) =>
    h("option", { value: v }, t)));
  const address = h("input", { type: "text", placeholder: "https://hooks.slack.com/services/…", "aria-label": "Address", size: 40 });
  const severity = h("select", { "aria-label": "At least" }, ["cosmetic", "additive", "semantic", "breaking"].map((v) =>
    h("option", { value: v, selected: v === "semantic" }, v)));
  scope.addEventListener("change", () => { project.hidden = scope.value !== "project"; });
  channel.addEventListener("change", () => {
    address.placeholder = { slack: "https://hooks.slack.com/services/…", email: "you@example.com", webhook: "https://…" }[channel.value];
  });
  const subscribe = async (event) => {
    event.preventDefault();
    const body = { channel: channel.value, address: address.value.trim(), min_severity: severity.value,
                   ...(scope.value === "project" ? { project: project.value.trim() } : scope.value === "owner" ? { owner: me.name } : { all: true }) };
    try { await api("/v1/alerts/subscriptions", { method: "POST", body }); route(); } catch (error) { message.textContent = error.message; }
  };
  const about = (s) => (s.scope === "project" ? entryLink(s.target, short(s.target)) : s.scope === "owner" ? `projects owned by ${s.target}` : "every project");
  return [h("h2", {}, "Alerts"),
    h("p", { class: "muted" }, "Be told when data a project uses changes after it used it, is gone from its source, or fails a data-quality test. ",
      "They are sent when ", h("code", {}, "odcl alerts send"), " runs, usually after the imports."),
    table(["About", "Sent to", "At least", "Delivery", me.role === "admin" ? "By" : null, ""].filter((x) => x !== null), subscriptions.map((s) => [
      about(s), `${s.channel} ${s.address}`, severityBadge(s.min_severity), deliveryBadge(s.delivery), ...(me.role === "admin" ? [s.created_by] : []),
      s.created_by === me.name || me.role === "admin" ? h("button", { type: "button", onclick: () => remove(s.id) }, "Remove") : ""]), "No alerts yet."),
    h("form", { class: "controls subscribe", onsubmit: subscribe }, field("About", scope), project, field("Send to", channel), address,
      field("At least", severity), h("button", { type: "submit" }, "Subscribe")),
    message];
}

async function accountPage() {
  const me = (await api("/v1/me")).user;
  if (!me) {
    render(h("h1", {}, "Account"), h("p", {}, "Not signed in. Reading needs no sign-in here; writing needs an API token, which an admin makes with ",
      h("code", {}, "odcl users token NAME"), "."),
      h("button", { type: "button", onclick: () => { const t = window.prompt("Paste an odcl API token:"); if (t) { sessionStorage.setItem(TOKEN, t.trim()); showAccount(); route(); } } },
        "Sign in with a token"));
    return;
  }
  const tokens = await api("/v1/me/tokens");
  const shown = h("div");
  const make = async () => {
    const label = window.prompt("What is this token for? (e.g. laptop, ci)") ?? undefined;
    const made = await api("/v1/me/tokens", { method: "POST", body: label ? { label } : {} });
    shown.replaceChildren(h("div", { class: "notice" }, h("p", {}, "Your new token. It is shown this once; keep it secret:"), h("pre", {}, made.token),
      h("p", { class: "muted" }, "Use it as ODCL_TOKEN for odcl and odcl mcp --server, or as Authorization: Bearer over HTTP.")));
  };
  const revoke = async (id) => { await api(`/v1/me/tokens/${enc(id)}/revoke`, { method: "POST" }); route(); };
  const parts = [h("h1", {}, me.display_name || me.name),
    h("p", { class: "sub" }, `${me.name} · ${me.role}`, me.email ? ` · ${me.email}` : "", me.single_sign_on ? " · signs in with single sign-on" : ""),
    h("div", { class: "controls" }, h("button", { type: "button", onclick: make }, "Make an API token"), h("button", { type: "button", onclick: signOut }, "Sign out")),
    shown, h("h2", {}, "Your tokens"),
    table(["Token", "For", "Made", "Last used", ""], tokens.map((t) => [h("code", {}, t.id), t.label || "—", when(t.created_at), when(t.last_used_at),
      h("button", { type: "button", onclick: () => revoke(t.id) }, "Revoke")]), "No tokens.")];
  if (me.role !== "reader") parts.push(...await alertsSection(me));
  if (me.role === "admin") {
    const everyone = await api("/v1/users");
    const set = async (name, change) => {
      try { await api(`/v1/users/${enc(name)}`, { method: "POST", body: change }); } catch (error) { window.alert(error.message); }
      route();
    };
    parts.push(h("h2", {}, "Users"), table(["User", "Role", "Email", "Last sign-in", "Tokens", ""], everyone.map((u) => [
      [u.display_name || u.name, u.display_name ? h("span", { class: "muted" }, ` ${u.name}`) : ""],
      u.name === me.name ? u.role : h("select", { "aria-label": `Role of ${u.name}`, onchange: (e) => set(u.name, { role: e.target.value }) },
        ["reader", "editor", "importer", "admin"].map((r) => h("option", { value: r, selected: r === u.role }, r))),
      u.email || "—", when(u.last_sign_in_at), u.tokens,
      u.name === me.name ? "" : h("button", { type: "button", onclick: () => set(u.name, { disabled: !u.disabled }) }, u.disabled ? "Enable" : "Disable")]), "No users."));
  }
  render(...parts);
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
    lineage: () => explorerPage(parts[1], params),
    model: () => modelPage(parts[1], parts[2]),
    dataset: () => datasetPage(parts.slice(1).join("/")),
    models: () => modelsPage(),
    tables: () => tablesPage(params),
    projects: () => projectsPage(),
    glossary: () => glossaryPage(),
    runs: () => runsPage(params),
    account: () => accountPage(),
  };
  const section = { "": "#/", models: "#/models", model: "#/models", tables: "#/tables", projects: "#/projects", glossary: "#/glossary", runs: "#/runs" }[parts[0] || ""];
  for (const link of document.querySelectorAll(".top nav a")) {
    if (link.getAttribute("href") === section) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
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
showAccount();
route();
