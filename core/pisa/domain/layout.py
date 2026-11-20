"""A diagram's layout, computed by Pisa's code, never the model or the phone.
Pure and deterministic: the same diagram gives the same bytes, so a saved view re-lays out identically.

- `flow` and `graph`: a layered layout (Sugiyama): cycles broken by reversing the edges a depth-first walk in
  node order finds going back; layers by longest path; long edges through dummy points; crossings reduced by
  barycentres in a fixed number of sweeps; coordinates packed per layer with each group kept contiguous and
  its box clear of every other node; edges as polylines from box border to box border.
- `layers`: stacked bands, one per group in the groups' order (the ungrouped nodes last, with no box), each
  band's nodes in a row in node order; edges straight, border to border.
- `direction`: "down" (layers top to bottom) or "right" (left to right).

No library: `grandalf` is GPL-2.0/EPL-1.0 and nothing MIT/Apache/BSD in Python does a layered layout for a
few dozen nodes; this is Pisa's own, ~300 lines.
"""

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

# The phone's text (IOS `PresentedSheet`): RealityType names no point sizes (it is Dynamic Type), so the
# spec's sizes at the default content size: 15 pt for a node's label, 13 pt for secondary text (an edge's or a
# group's label; a node's detail shows on a long press, never inside the box). The advance is SF Pro Text's
# average glyph width as a share of the size, rounded up so a label never comes out wider than computed.
LABEL_PT = 15.0
SECONDARY_PT = 13.0
ADVANCE_EM = 0.56
LINE_EM = 1.25
LABEL_LINES = 3  # a label wraps to at most this many lines, or it is cut (the render check drops the block)

NODE_PAD_X, NODE_PAD_Y = 12.0, 10.0
NODE_MIN_W, NODE_MAX_W = 88.0, 200.0
ACTOR_EXTRA_H = 18.0  # an actor's figure above its label
GAP_CROSS = 24.0  # between neighbours in a layer
GAP_MAIN = 56.0  # between layers (room for an edge's label and a group's padding)
GROUP_PAD = 10.0
GROUP_LABEL = SECONDARY_PT * LINE_EM + 4.0  # the strip a group's label takes at its box's top
MARGIN = 8.0
SWEEPS = 8  # barycentre sweeps (down, up, down, …): fixed, so the result never depends on time
BAND_GAP = 24.0

# The render check (the phone's width): at its minimum scale the diagram fits a 390 pt phone inside the
# sheet's 16 pt gutters, or the phone pans it (`scroll`).
PHONE_PT = 390.0
SHEET_GUTTER = 16.0
MIN_SCALE = 0.6


def text_width(text: str, size: float) -> float:
    return len(text) * size * ADVANCE_EM


def wrap(text: str, width: float, size: float = LABEL_PT) -> list[str] | None:
    """Greedy word wrap at `width`; None when a word alone is wider (it would be cut)."""
    lines: list[str] = []
    for word in text.split():
        if text_width(word, size) > width:
            return None
        if lines and text_width(f"{lines[-1]} {word}", size) <= width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines or [""]


def node_size(label: str, shape: str) -> tuple[float, float] | None:
    """A node's box (w, h) for its label, or None when the label can't show whole (LABEL_LINES lines of
    NODE_MAX_W): the render check's "no label is cut"."""
    inner_max = NODE_MAX_W - 2 * NODE_PAD_X
    lines = wrap(label, inner_max)
    if lines is None or len(lines) > LABEL_LINES:
        return None
    widest = max(text_width(line, LABEL_PT) for line in lines)
    w = max(NODE_MIN_W, math.ceil(widest + 2 * NODE_PAD_X))
    h = math.ceil(len(lines) * LABEL_PT * LINE_EM + 2 * NODE_PAD_Y)
    if shape == "actor":
        h += ACTOR_EXTRA_H
    elif shape == "store":
        h += 8.0  # a cylinder's rims
    return float(w), float(h)


def fits_phone(width: float, phone: float = PHONE_PT) -> bool:
    return width * MIN_SCALE <= phone - 2 * SHEET_GUTTER


def phone_width(phone: float | None) -> float:
    """The width a diagram is laid out for: the phone's (its hello), else PHONE_PT."""
    return phone if phone is not None and 2 * SHEET_GUTTER < phone <= 2000 else PHONE_PT


def _scale(width: float, phone: float) -> float:
    """The scale the phone draws a diagram this wide at (1 at most): its sheet's width over the diagram's."""
    return min(1.0, (phone - 2 * SHEET_GUTTER) / width) if width > 0 else 1.0


def fitted(diagram: dict[str, Any], phone: float | None = None) -> tuple[dict[str, Any], str]:
    """The layout and its
    direction for the phone's width (its hello's `surface.width`, else PHONE_PT). The direction asked stays
    when it shows at full size; otherwise the other one is laid out too and the one drawn larger wins (on a
    portrait phone a chain goes top to bottom), the one asked on a tie. Raises Uncut as `layout`."""
    width = phone_width(phone)
    asked = "right" if diagram.get("direction") == "right" else "down"
    first = layout({**diagram, "direction": asked})
    if _scale(first["width"], width) >= 1.0:
        return first, asked
    other = "down" if asked == "right" else "right"
    second = layout({**diagram, "direction": other})
    if _scale(second["width"], width) > _scale(first["width"], width):
        return second, other
    return first, asked


class Uncut(ValueError):
    """A label that can't show whole: the diagram is dropped and named."""


@dataclass
class _Item:
    key: str  # a node's id, or "d<edge>:<layer>" for a long edge's dummy point
    layer: int
    cross: float = 0.0  # its size across the layer
    main: float = 0.0  # its size along the layers
    group: str | None = None
    pos: float = 0.0  # its x (down) or y (right): the top-left corner on the cross axis


@dataclass
class _Graph:
    items: dict[str, _Item] = field(default_factory=dict)
    layers: list[list[str]] = field(default_factory=list)
    chains: list[list[str]] = field(default_factory=list)  # per edge: the items it passes, source to target
    reversed_: list[bool] = field(default_factory=list)


def _r(value: float) -> float:
    return round(value, 1)


def layout(diagram: dict[str, Any]) -> dict[str, Any]:
    """The `layout` of a diagram block (its nodes, edges, groups, style, direction), in points at scale 1.
    Raises Uncut when a label can't show whole."""
    nodes: list[dict[str, Any]] = diagram["nodes"]
    sizes: dict[str, tuple[float, float]] = {}
    for n in nodes:
        size = node_size(str(n["label"]), str(n.get("shape") or "box"))
        if size is None:
            raise Uncut(f"node {n['id']}: its label doesn't fit {LABEL_LINES} lines")
        sizes[n["id"]] = size
    right = diagram.get("direction") == "right"
    if diagram.get("style") == "layers":
        return _bands(diagram, sizes, right)
    return _layered(diagram, sizes, right)


def _layered(diagram: dict[str, Any], sizes: dict[str, tuple[float, float]], right: bool) -> dict[str, Any]:
    nodes = [n["id"] for n in diagram["nodes"]]
    group_of = {n["id"]: n.get("group") for n in diagram["nodes"]}
    edges = [(e["from"], e["to"]) for e in diagram["edges"]]
    flipped = _feedback(nodes, edges)
    directed = [(b, a) if flipped[i] else (a, b) for i, (a, b) in enumerate(edges)]
    layer = _layering(nodes, directed)
    g = _Graph(reversed_=flipped)
    depth = max(layer.values()) + 1
    g.layers = [[] for _ in range(depth)]
    for n in nodes:  # node order: the initial order in each layer
        w, h = sizes[n]
        g.items[n] = _Item(n, layer[n], h if right else w, w if right else h, group_of[n])
        g.layers[layer[n]].append(n)
    for i, (a, b) in enumerate(directed):
        chain = [a]
        for lay in range(layer[a] + 1, layer[b]):
            key = f"d{i}:{lay}"
            g.items[key] = _Item(key, lay, 0.0, 0.0)
            g.layers[lay].append(key)
            chain.append(key)
        chain.append(b)
        g.chains.append(chain)
    _order(g)
    group_order = [gr["id"] for gr in diagram.get("groups") or []]
    _grouped(g, group_order)
    _place(g, group_order, GROUP_LABEL if right else 0.0)
    return _emit(g, diagram, right)


def _feedback(nodes: list[str], edges: list[tuple[str, str]]) -> list[bool]:
    """Which edges a depth-first walk in node order finds going back (reversed to break cycles); self-loops
    count as back edges too (the caller drops them first)."""
    out: dict[str, list[int]] = {n: [] for n in nodes}
    for i, (a, _) in enumerate(edges):
        out[a].append(i)
    state = dict.fromkeys(nodes, 0)  # 0 new, 1 on the path, 2 done
    back = [False] * len(edges)
    for root in nodes:
        if state[root]:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        state[root] = 1
        while stack:
            node, k = stack[-1]
            if k < len(out[node]):
                stack[-1] = (node, k + 1)
                i = out[node][k]
                target = edges[i][1]
                if state[target] == 1:
                    back[i] = True
                elif state[target] == 0:
                    state[target] = 1
                    stack.append((target, 0))
            else:
                state[node] = 2
                stack.pop()
    return back


def _layering(nodes: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest path from the sources (the edges are acyclic now): each node one layer past its deepest
    predecessor."""
    preds: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        preds[b].append(a)
    layer: dict[str, int] = {}

    def depth(n: str) -> int:
        stack = [n]
        while stack:
            top = stack[-1]
            pending = [p for p in preds[top] if p not in layer]
            if pending:
                stack.extend(pending)
                continue
            stack.pop()
            if top not in layer:
                layer[top] = max((layer[p] + 1 for p in preds[top]), default=0)
        return layer[n]

    for n in nodes:
        depth(n)
    return layer


def _neighbours(g: _Graph) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    up: dict[str, list[str]] = {k: [] for k in g.items}
    down: dict[str, list[str]] = {k: [] for k in g.items}
    for chain in g.chains:
        for a, b in pairwise(chain):
            down[a].append(b)
            up[b].append(a)
    return up, down


def _crossings(g: _Graph, down: dict[str, list[str]]) -> int:
    total = 0
    for lay in range(len(g.layers) - 1):
        at = {k: i for i, k in enumerate(g.layers[lay + 1])}
        pairs = [(i, at[t]) for i, k in enumerate(g.layers[lay]) for t in down[k]]
        total += sum(
            1 for x, (a1, b1) in enumerate(pairs) for a2, b2 in pairs[x + 1 :] if (a1 - a2) * (b1 - b2) < 0
        )
    return total


def _order(g: _Graph) -> None:
    """Crossing reduction: barycentres, SWEEPS sweeps alternating down and up; the best order seen wins (the
    first of equals: deterministic)."""
    up, down = _neighbours(g)
    best = [list(layer) for layer in g.layers]
    best_count = _crossings(g, down)
    for sweep in range(SWEEPS):
        going_down = sweep % 2 == 0
        span = range(1, len(g.layers)) if going_down else range(len(g.layers) - 2, -1, -1)
        for lay in span:
            fixed = g.layers[lay - 1] if going_down else g.layers[lay + 1]
            at = {k: i for i, k in enumerate(fixed)}
            near = up if going_down else down
            current = g.layers[lay]

            def bary(k: str, i: int, near: dict[str, list[str]] = near, at: dict[str, int] = at) -> float:
                linked = [at[n] for n in near[k] if n in at]
                return sum(linked) / len(linked) if linked else float(i)

            g.layers[lay] = [k for _, _, k in sorted((bary(k, i), i, k) for i, k in enumerate(current))]
        count = _crossings(g, down)
        if count < best_count:
            best, best_count = [list(layer) for layer in g.layers], count
    g.layers = best


def _grouped(g: _Graph, group_order: list[str]) -> None:
    """Each group contiguous in every layer, in one order across layers (its box then holds no other node):
    a cluster's key is its members' mean relative position; a lone node (or dummy) is its own cluster."""
    rel: dict[str, float] = {}
    for layer in g.layers:
        for i, k in enumerate(layer):
            rel[k] = (i + 0.5) / len(layer)
    keys: dict[str, float] = {}
    for gid in group_order:
        members = [rel[k] for k, it in g.items.items() if it.group == gid]
        if members:
            keys[gid] = sum(members) / len(members)

    def cluster(k: str) -> tuple[float, int, str]:
        item = g.items[k]
        if item.group is not None and item.group in keys:
            return (keys[item.group], 0, item.group)
        return (rel[k], 1, k)

    g.layers = [sorted(layer, key=lambda k: (cluster(k), rel[k], k)) for layer in g.layers]


def _place(g: _Graph, group_order: list[str], head: float) -> None:
    """Cross-axis positions, two packing passes: first tight, then each lone item pulled toward its
    neighbours' centre. A group takes one column across the layers it spans, clear of every other item."""
    up, down = _neighbours(g)
    desired: dict[str, float] = {}
    for _ in range(2):
        _pack(g, group_order, desired, head)
        centre = {k: it.pos + it.cross / 2 for k, it in g.items.items()}
        desired = {}
        for k, it in g.items.items():
            linked = [centre[n] for n in up[k] + down[k]]
            if it.group is None and linked:
                desired[k] = sum(linked) / len(linked) - it.cross / 2


def _pack(g: _Graph, group_order: list[str], desired: dict[str, float], head: float) -> None:
    """`head`: a group's label strip when it lies on the cross axis ("right")."""
    cursor = [MARGIN] * len(g.layers)
    seen_groups: set[str] = set()
    index = {k: i for layer in g.layers for i, k in enumerate(layer)}
    # the clusters in one global order: a layer's sequence is that order restricted (`_grouped`)
    sequence: list[tuple[float, str]] = []
    for layer in g.layers:
        for k in layer:
            it = g.items[k]
            cl = it.group if it.group is not None and it.group in group_order else None
            if cl is None:
                sequence.append((0.0, k))
            elif cl not in seen_groups:
                seen_groups.add(cl)
                sequence.append((0.0, f"group:{cl}"))
    order = _global(g, sequence, index)
    for name in order:
        if name.startswith("group:"):
            gid = name[6:]
            members = [k for k, it in g.items.items() if it.group == gid]
            lays = sorted({g.items[k].layer for k in members})
            span = range(lays[0], lays[-1] + 1)
            left = max(cursor[lay] for lay in span) + GROUP_PAD + head
            end = left
            for lay in lays:
                x = left
                for k in [k for k in g.layers[lay] if g.items[k].group == gid]:
                    g.items[k].pos = x
                    x += g.items[k].cross + GAP_CROSS
                end = max(end, x - GAP_CROSS)
            for lay in span:
                cursor[lay] = end + GROUP_PAD + GAP_CROSS
        else:
            it = g.items[name]
            it.pos = max(cursor[it.layer], desired.get(name, cursor[it.layer]))
            cursor[it.layer] = it.pos + it.cross + (GAP_CROSS if it.cross else GAP_CROSS / 2)


def _global(g: _Graph, sequence: list[tuple[float, str]], index: dict[str, int]) -> list[str]:
    """One order of the clusters that every layer's own order respects: a topological order of "comes
    before in some layer", ties by first appearance."""
    names = [n for _, n in sequence]
    rank = {n: i for i, n in enumerate(names)}

    def name_of(k: str) -> str:
        it = g.items[k]
        return f"group:{it.group}" if it.group is not None and f"group:{it.group}" in rank else k

    after: dict[str, set[str]] = {n: set() for n in names}
    for layer in g.layers:
        cl = [name_of(k) for k in layer]
        for a, b in pairwise(cl):
            if a != b:
                after[a].add(b)
    incoming = {n: 0 for n in names}
    for a in names:
        for b in after[a]:
            incoming[b] += 1
    ready = sorted((rank[n], n) for n in names if incoming[n] == 0)
    out: list[str] = []
    while ready:
        _, n = ready.pop(0)
        out.append(n)
        for b in sorted(after[n], key=lambda m: rank[m]):
            incoming[b] -= 1
            if incoming[b] == 0:
                ready.append((rank[b], b))
                ready.sort()
    out += [n for n in names if n not in out]  # never happens with contiguous groups; keeps every item
    return out


def _emit(g: _Graph, diagram: dict[str, Any], right: bool) -> dict[str, Any]:
    thickness = [max((g.items[k].main for k in layer), default=0.0) for layer in g.layers]
    main_at, at = [], MARGIN + GROUP_PAD + (GROUP_LABEL if not right else 0.0)
    for t in thickness:
        main_at.append(at)
        at += t + GAP_MAIN
    boxes: dict[str, tuple[float, float, float, float]] = {}  # (x, y, w, h)
    centres: dict[str, tuple[float, float]] = {}
    for k, it in g.items.items():
        lay_main = main_at[it.layer] + (thickness[it.layer] - it.main) / 2
        cross = it.pos
        if right:
            boxes[k] = (lay_main, cross, it.main, it.cross)
        else:
            boxes[k] = (cross, lay_main, it.cross, it.main)
        x, y, w, h = boxes[k]
        centres[k] = (x + w / 2, y + h / 2) if it.cross else (x, main_at[it.layer] + thickness[it.layer] / 2)
        if not it.cross and right:
            centres[k] = (main_at[it.layer] + thickness[it.layer] / 2, y)
    nodes = {n["id"]: _box(*boxes[n["id"]]) for n in diagram["nodes"]}
    groups: dict[str, dict[str, float]] = {}
    for gr in diagram.get("groups") or []:
        member = [boxes[n["id"]] for n in diagram["nodes"] if n.get("group") == gr["id"]]
        if member:
            x0 = min(b[0] for b in member) - GROUP_PAD
            y0 = min(b[1] for b in member) - GROUP_PAD - GROUP_LABEL
            x1 = max(b[0] + b[2] for b in member) + GROUP_PAD
            y1 = max(b[1] + b[3] for b in member) + GROUP_PAD
            groups[gr["id"]] = _box(x0, y0, x1 - x0, y1 - y0)
    edges = []
    for i, chain in enumerate(g.chains):
        a, b = chain[0], chain[-1]
        inner = [centres[k] for k in chain[1:-1]]
        start = _border(boxes[a], inner[0] if inner else centres[b])
        end = _border(boxes[b], inner[-1] if inner else centres[a])
        points = [start, *inner, end]
        if g.reversed_[i]:
            points.reverse()
        edges.append({"points": [[_r(x), _r(y)] for x, y in points]})
    xs = [b[0] + b[2] for b in boxes.values()] + [v["x"] + v["w"] for v in groups.values()]
    ys = [b[1] + b[3] for b in boxes.values()] + [v["y"] + v["h"] for v in groups.values()]
    out: dict[str, Any] = {"width": _r(max(xs) + MARGIN), "height": _r(max(ys) + MARGIN), "nodes": nodes,
                           "edges": edges}  # fmt: skip
    if groups:
        out["groups"] = groups
    return out


def _box(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": _r(x), "y": _r(y), "w": _r(w), "h": _r(h)}


def _border(box: tuple[float, float, float, float], toward: tuple[float, float]) -> tuple[float, float]:
    """Where the line from the box's centre toward a point leaves the box."""
    x, y, w, h = box
    cx, cy = x + w / 2, y + h / 2
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        return (cx, y + h)
    scale = min(w / 2 / abs(dx) if dx else math.inf, h / 2 / abs(dy) if dy else math.inf)
    return (cx + dx * scale, cy + dy * scale)


def _bands(diagram: dict[str, Any], sizes: dict[str, tuple[float, float]], right: bool) -> dict[str, Any]:
    """`layers`: one band per group (in the groups' order), the ungrouped nodes last with no box."""
    order = [gr["id"] for gr in diagram.get("groups") or []]
    bands: list[tuple[str | None, list[str]]] = [
        (gid, [n["id"] for n in diagram["nodes"] if n.get("group") == gid]) for gid in order
    ]
    bands = [b for b in bands if b[1]]
    loose = [n["id"] for n in diagram["nodes"] if n.get("group") not in order]
    if loose:
        bands.append((None, loose))
    boxes: dict[str, tuple[float, float, float, float]] = {}
    band_boxes: dict[str, tuple[float, float, float, float]] = {}
    main = MARGIN
    rows: list[tuple[str | None, float, float]] = []  # (group, its main start, thickness)
    widest = 0.0
    for gid, members in bands:
        thick = max(sizes[m][0] if right else sizes[m][1] for m in members)
        head = GROUP_LABEL if gid is not None else 0.0
        start = main + (GROUP_PAD + head if gid is not None else 0.0)
        cross = MARGIN + GROUP_PAD
        for m in members:
            w, h = sizes[m]
            along = h if right else w
            mx = start + (thick - (w if right else h)) / 2
            boxes[m] = (mx, cross, w, h) if right else (cross, mx, w, h)
            cross += along + GAP_CROSS
        widest = max(widest, cross - GAP_CROSS + GROUP_PAD)
        rows.append((gid, main, (start - main) + thick + (GROUP_PAD if gid is not None else 0.0)))
        main += rows[-1][2] + BAND_GAP
    for gid, start, thick in rows:
        if gid is not None:
            band_boxes[gid] = (
                (start, MARGIN, thick, widest - MARGIN) if right else (MARGIN, start, widest - MARGIN, thick)
            )
    centre = {k: (b[0] + b[2] / 2, b[1] + b[3] / 2) for k, b in boxes.items()}
    edges = []
    for e in diagram["edges"]:
        a, b = e["from"], e["to"]
        points = [_border(boxes[a], centre[b]), _border(boxes[b], centre[a])]
        edges.append({"points": [[_r(x), _r(y)] for x, y in points]})
    extent_main = main - BAND_GAP + MARGIN
    extent_cross = widest + MARGIN
    out: dict[str, Any] = {
        "width": _r(extent_main if right else extent_cross),
        "height": _r(extent_cross if right else extent_main),
        "nodes": {n["id"]: _box(*boxes[n["id"]]) for n in diagram["nodes"]},
        "edges": edges,
    }
    if band_boxes:
        out["groups"] = {gid: _box(*b) for gid, b in band_boxes.items()}
    return out
