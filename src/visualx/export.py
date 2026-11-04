"""Exports of the graph without signal states (FR-51, BSC-05).

Two formats:

* **GEXF 1.3** (one ``.gexf`` file) for Gephi and other graph tools. Nodes have the
  handle as the label and the metrics and groups of the analyze stage as attributes.
  Edges have the weight and the count for each interaction type.
* **x-network-map JSON** (``interactions.json`` and ``profiles.json`` shapes of
  poseljacob/x-network-map, research V2-R12): ``[{from, to, type, count, last}]``
  and ``{handle: {name, bio, followers, img}}``. ``img`` is always null: the export
  holds no image URL (SR-03).

This module reads ``nodes``, ``edges``, ``node_groups``, and ``profiles`` only. It
never reads the score stage tables or the owner overrides (invariant 8, BSC-05:
``tools/release_checks.py`` refuses an export module that names them). Each file goes into the ``exports`` table with its user IDs
in ``export_members``, so a deletion finds and deletes it (AD-10, invariant 11).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .config import Config
from .logs import RunLog
from .model import INTERACTION_TYPES
from .store import Store, new_id, now_iso

FORMATS = ("gexf", "xnm")
# XML 1.0 does not allow these characters. Text from X can hold them.
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


class ExportError(RuntimeError):
    pass


def _clean(v: Any) -> str:
    return _XML_BAD.sub("", "" if v is None else str(v))


def _rows(store: Store) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    nodes = [dict(r) for r in store.q(
        "SELECT user_id, handle, name, is_ego, degree, weighted_degree, pagerank, betweenness, x, y "
        "FROM nodes ORDER BY user_id")]
    if not nodes:
        raise ExportError("the nodes table is empty. Run 'visualx build' and 'visualx analyze' first.")
    edges = [dict(r) for r in store.q(
        "SELECT source, target, reply_count, quote_count, mention_count, repost_count, weight, pair_weight, "
        "timestamps_json FROM edges ORDER BY source, target")]
    groups = {r["user_id"]: dict(r) for r in store.q(
        "SELECT user_id, community, stability, is_hub, is_bridge, topics_json FROM node_groups")}
    return nodes, edges, groups


def _topics(raw: Any) -> str:
    try:
        t = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return ""
    names = list(t.get("direct") or [])
    if t.get("inferred"):
        names.append(str(t["inferred"]) + " (from links)")
    return ", ".join(str(x) for x in names)


NODE_ATTRS = (
    ("handle", "string"), ("name", "string"), ("is_owner", "boolean"), ("degree", "integer"),
    ("weighted_degree", "double"), ("pagerank", "double"), ("betweenness", "double"),
    ("community", "integer"), ("stability", "double"), ("hub", "boolean"), ("bridge", "boolean"),
    ("topics", "string"),
)
EDGE_ATTRS = tuple((f"{t}_count", "integer") for t in INTERACTION_TYPES) + (("pair_weight", "double"),)


def gexf_text(store: Store) -> tuple[str, list[str]]:
    """Return (GEXF document, user IDs in it)."""
    nodes, edges, groups = _rows(store)
    root = ET.Element("gexf", {"xmlns": "http://gexf.net/1.3", "version": "1.3"})
    meta = ET.SubElement(root, "meta", {"lastmodifieddate": now_iso()[:10]})
    ET.SubElement(meta, "creator").text = "VisualX_AI"
    ET.SubElement(meta, "description").text = "Interaction graph of the owner's X account (VisualX_AI export, FR-51)."
    g = ET.SubElement(root, "graph", {"defaultedgetype": "directed", "mode": "static"})
    na = ET.SubElement(g, "attributes", {"class": "node"})
    for k, (name, typ) in enumerate(NODE_ATTRS):
        ET.SubElement(na, "attribute", {"id": str(k), "title": name, "type": typ})
    ea = ET.SubElement(g, "attributes", {"class": "edge"})
    for k, (name, typ) in enumerate(EDGE_ATTRS):
        ET.SubElement(ea, "attribute", {"id": str(k), "title": name, "type": typ})
    ns = ET.SubElement(g, "nodes")
    for r in nodes:
        gr = groups.get(r["user_id"], {})
        vals = {
            "handle": r["handle"], "name": r["name"], "is_owner": bool(r["is_ego"]), "degree": r["degree"],
            "weighted_degree": r["weighted_degree"], "pagerank": r["pagerank"], "betweenness": r["betweenness"],
            "community": gr.get("community"), "stability": gr.get("stability"),
            "hub": bool(gr.get("is_hub")), "bridge": bool(gr.get("is_bridge")),
            "topics": _topics(gr.get("topics_json")),
        }
        nd = ET.SubElement(ns, "node", {"id": _clean(r["user_id"]), "label": _clean(r["handle"] or r["user_id"])})
        av = ET.SubElement(nd, "attvalues")
        for k, (name, typ) in enumerate(NODE_ATTRS):
            v = vals[name]
            if v is None or v == "":
                continue
            text = ("true" if v else "false") if typ == "boolean" else _clean(v)
            ET.SubElement(av, "attvalue", {"for": str(k), "value": text})
        if r["x"] is not None and r["y"] is not None:
            # A literal prefix: the root declares xmlns:viz below.
            ET.SubElement(nd, "viz:position", {"x": repr(float(r["x"])), "y": repr(float(r["y"])), "z": "0.0"})
    es = ET.SubElement(g, "edges")
    for k, e in enumerate(edges):
        ed = ET.SubElement(es, "edge", {"id": str(k), "source": _clean(e["source"]), "target": _clean(e["target"]),
                                        "weight": repr(float(e["weight"]))})
        av = ET.SubElement(ed, "attvalues")
        for a, t in enumerate(INTERACTION_TYPES):
            ET.SubElement(av, "attvalue", {"for": str(a), "value": str(int(e[f"{t}_count"]))})
        ET.SubElement(av, "attvalue", {"for": str(len(INTERACTION_TYPES)), "value": repr(float(e["pair_weight"]))})
    root.set("xmlns:viz", "http://gexf.net/1.3/viz")
    ET.indent(root)
    text = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    return text, [r["user_id"] for r in nodes]


def xnm_docs(store: Store) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    """Return (interactions, profiles, user IDs) in the x-network-map shapes."""
    nodes, edges, _ = _rows(store)
    profiles = store.profiles()
    key = {r["user_id"]: (r["handle"] or r["user_id"]) for r in nodes}
    inter: list[dict[str, Any]] = []
    for e in edges:
        try:
            times = json.loads(e["timestamps_json"] or "{}")
        except json.JSONDecodeError:
            times = {}
        for t in INTERACTION_TYPES:
            c = int(e[f"{t}_count"])
            if c <= 0:
                continue
            ts = sorted(x for x in (times.get(t) or []) if isinstance(x, str))
            inter.append({"from": key[e["source"]], "to": key[e["target"]], "type": t, "count": c,
                          "last": ts[-1] if ts else None})
    prof: dict[str, dict[str, Any]] = {}
    for r in nodes:
        p = profiles.get(r["user_id"]) or {}
        prof[key[r["user_id"]]] = {"name": r["name"] or p.get("name"), "bio": p.get("description"),
                                   "followers": p.get("followers_count"), "img": None}
    return inter, prof, [r["user_id"] for r in nodes]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def run_export(cfg: Config, store: Store, log: RunLog, fmt: str, out_dir: Path | None = None) -> list[Path]:
    """Write the export files and record them for deletion. Return the paths."""
    if fmt not in FORMATS:
        raise ExportError(f"format must be one of {FORMATS}, not {fmt!r}")
    if store.latest_stage_run("analyze") is None:
        raise ExportError("no analyze output. Run 'visualx analyze' first.")
    out = Path(out_dir) if out_dir is not None else cfg.data_dir / "exports"
    eid = new_id("export")
    files: list[tuple[Path, str]] = []
    if fmt == "gexf":
        text, ids = gexf_text(store)
        files.append((out / f"{eid}.gexf", text))
    else:
        inter, prof, ids = xnm_docs(store)
        files.append((out / f"{eid}_interactions.json", json.dumps(inter, ensure_ascii=False, indent=1)))
        files.append((out / f"{eid}_profiles.json", json.dumps(prof, ensure_ascii=False, indent=1)))
    paths = []
    with store.tx() as c:
        for k, (path, text) in enumerate(files):
            _write(path, text)
            fid = f"{eid}_{k}"
            c.execute("INSERT INTO exports(export_id, path, format, created_at, stage_run_id) VALUES (?,?,?,?,?)",
                      (fid, str(path.resolve()), fmt, now_iso(), store.latest_stage_run("analyze")["stage_run_id"]))
            c.executemany("INSERT INTO export_members(export_id, user_id) VALUES (?,?)", [(fid, u) for u in ids])
            paths.append(path)
    log.write("export", format=fmt, files=len(paths), nodes=len(ids))
    return paths
