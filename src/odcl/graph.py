"""Lineage around a table or column as a graph: every node it reaches and every edge between them.

`Catalog.lineage` lists what is upstream and downstream, each entry once with the step that first reached it. A
graph keeps every edge, so where two inputs meet, or one input feeds many outputs, it shows. Each node has its
`side` (-1 upstream, 0 the focus, 1 downstream) and `depth`, the fewest steps from the focus. Edges run from input
to output: `derived_from` lineage, `copy` (a copy of a definition is built from it) and, with `projects`, `uses`.
At `level="table"`, columns are folded into their tables.
"""
from __future__ import annotations

from collections import deque

from .store import Catalog, require

LIMIT = 400  # nodes a graph returns; past it, the nearest are kept and `truncated` says so


def graph(catalog: Catalog, name: str, direction: str = "both", depth: int | None = None, projects: bool = False, level: str = "column") -> dict:
    require(direction in ("up", "down", "both"), "direction", "direction is up, down or both")
    require(depth is None or (isinstance(depth, int) and 1 <= depth <= 50), "depth", "depth is between 1 and 50")
    require(level in ("column", "table"), "level", "level is column or table")
    focus = catalog.resolve(name, ("table", "column"))
    # a table's lineage at table level is also its columns': a dbt or SQL import often links only the columns
    starts = [focus, *catalog._columns(focus, retired=False)] if level == "table" and focus.startswith("table:") else [focus]
    place: dict[str, tuple[int, int]] = {k: (0, 0) for k in starts}  # key -> (side, depth)
    edges: dict[tuple[str, str], dict] = {}
    for walk, side in (("up", -1), ("down", 1)):
        if direction not in (walk, "both"):
            continue
        rows = [r for start in starts for r in catalog._walk_edges(start, walk)]
        near: dict[str, list] = {}
        for r in rows:
            near.setdefault(r["near"], []).append(r)
        reached, queue = {k: 0 for k in starts}, deque(starts)
        while queue:
            current = queue.popleft()
            if depth is not None and reached[current] >= depth:
                continue
            for r in near.get(current, []):
                if r["far"] not in reached:
                    reached[r["far"]] = reached[current] + 1
                    queue.append(r["far"])
        for key, d in reached.items():
            place.setdefault(key, (side, d))
        for r in rows:
            if r["near"] in reached and r["far"] in reached:
                pair = (r["far"], r["near"]) if walk == "up" else (r["near"], r["far"])  # input -> output
                edges[pair] = {"kind": "copy" if r["copy"] else "derived_from", "changed_since": r["target_revision"] != r["target_head"]}
    truncated = len(place) > LIMIT
    if truncated:
        kept = sorted(place, key=lambda k: (place[k][1], k))[:LIMIT]
        place = {k: place[k] for k in kept}
        edges = {pair: e for pair, e in edges.items() if pair[0] in place and pair[1] in place}

    if projects:  # a project uses a table or column; at table level, its uses of columns fold into their tables below
        for r in catalog.db.execute("""SELECT source_key, target_key, target_revision <> target_head AS changed FROM current_links
                                       WHERE relation = 'uses' AND target_key = ANY(%s)""", (list(place),)).fetchall():
            side, d = place[r["target_key"]]
            place.setdefault(r["source_key"], (1, max(d, 0) + 1 if side >= 0 else 1))
            edges[(r["target_key"], r["source_key"])] = {"kind": "uses", "changed_since": r["changed"]}

    if level == "table":
        heads = catalog._heads(list(place))
        table_of = {k: (heads[k]["body"]["table"] if heads[k]["body"]["kind"] == "column" else k) for k in place}
        folded: dict[str, tuple[int, int]] = {}
        for k, (side, d) in place.items():
            t = table_of[k]
            if t not in folded or (d, side != 0) < (folded[t][1], folded[t][0] != 0):
                folded[t] = (side, d)
        folded[table_of[focus]] = (0, 0)
        merged: dict[tuple[str, str], dict] = {}
        for (a, b), e in edges.items():
            pair = (table_of[a], table_of[b])
            if pair[0] != pair[1]:
                was = merged.get(pair)
                merged[pair] = {"kind": e["kind"] if not was or was["kind"] == e["kind"] else "derived_from",
                                "changed_since": e["changed_since"] or bool(was and was["changed_since"])}
        place, edges, focus = folded, merged, table_of[focus]

    summaries = {s["key"]: s for s in catalog._summaries(list(place))}
    nodes = [{**summaries[k], "side": side, "depth": d} for k, (side, d) in sorted(place.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))]
    return {"focus": focus, "direction": direction, "depth": depth, "level": level, "nodes": nodes,
            "edges": [{"from": a, "to": b, **e} for (a, b), e in sorted(edges.items())], "truncated": truncated}
