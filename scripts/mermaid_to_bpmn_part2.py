"""
mermaid_to_bpmn_part2.py --- Layout Engine + BPMN XML Generator (Part 2 of 2)

Builds on Part 1 (mermaid_to_bpmn_part1.py):
  - parse_mermaid(text) -> (nodes, edges, flow_name)
  - auto_map_elements(nodes, edges) -> (bpmn_nodes, bpmn_edges, explanations)
  - get_element_dimensions(bpmn_type) -> (width, height)

This module provides:
  1. auto_layout()          - Topological sort + automatic node positioning
  2. build_bpmn_xml()       - Generate complete BPMN 2.0 XML string
  3. format_bpmn_xml()      - Pretty-print XML
  4. calculate_waypoints()  - Edge waypoint coordinate calculation

Uses only Python stdlib (xml.etree.ElementTree, xml.dom.minidom).
"""

import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
from collections import defaultdict, deque

# ---------------------------------------------------------------------------
# Namespace constants
# ---------------------------------------------------------------------------

NS_BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
NS_BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
NS_DC = "http://www.omg.org/spec/DD/20100524/DC"
NS_DI = "http://www.omg.org/spec/DD/20100524/DI"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
NS_MODELER = "http://camunda.org/schema/Modeler/1.0"

# Register namespaces so ElementTree uses the correct prefixes
ET.register_namespace("bpmn", NS_BPMN)
ET.register_namespace("bpmndi", NS_BPMNDI)
ET.register_namespace("dc", NS_DC)
ET.register_namespace("di", NS_DI)
ET.register_namespace("xsi", NS_XSI)
ET.register_namespace("modeler", NS_MODELER)

# ---------------------------------------------------------------------------
# Default dimensions (mirrors Part 1's get_element_dimensions)
# ---------------------------------------------------------------------------

_DIMENSION_MAP: dict[str, tuple[int, int]] = {
    "bpmn:startEvent": (36, 36),
    "bpmn:endEvent": (36, 36),
    "bpmn:intermediateCatchEvent": (36, 36),
    "bpmn:exclusiveGateway": (50, 50),
    "bpmn:parallelGateway": (50, 50),
    "bpmn:inclusiveGateway": (50, 50),
    "bpmn:complexGateway": (50, 50),
    "bpmn:eventBasedGateway": (50, 50),
    "bpmn:subProcess": (140, 100),
    "bpmn:task": (100, 80),
    "bpmn:userTask": (100, 80),
    "bpmn:serviceTask": (100, 80),
    "bpmn:sendTask": (100, 80),
    "bpmn:receiveTask": (100, 80),
    "bpmn:businessRuleTask": (100, 80),
    "bpmn:scriptTask": (100, 80),
    "bpmn:manualTask": (100, 80),
}

# Spacing constants
COL_SPACING = 220
ROW_SPACING = 120


def _get_dim(bpmn_type: str) -> tuple[int, int]:
    """Return (width, height) for the given BPMN type."""
    return _DIMENSION_MAP.get(bpmn_type, (100, 80))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def auto_layout(
    bpmn_nodes: dict,
    bpmn_edges: list,
) -> dict:
    """
    Perform automatic layout via topological sort + column positioning.

    Parameters:
        bpmn_nodes: dict of id -> {id, bpmn_type, name, ...}
        bpmn_edges: list of {id, src, tgt, label, condition}

    Returns:
        dict of node_id -> {
            "x": center_x,
            "y": center_y,
            "layer": N,
            "col": M,
            "left_branch_ids": list (for gateways),
            "right_branch_ids": list (for gateways),
        }
    """
    if not bpmn_nodes:
        return {}

    # Build adjacency structures
    adj: dict[str, list[str]] = defaultdict(list)       # src -> [tgt, ...]
    incoming: dict[str, list[str]] = defaultdict(list)   # tgt -> [src, ...]
    all_node_ids: set[str] = set(bpmn_nodes.keys())

    for edge in bpmn_edges:
        src = edge["src"]
        tgt = edge["tgt"]
        if src in all_node_ids and tgt in all_node_ids:
            adj[src].append(tgt)
            incoming[tgt].append(src)

    # Compute out-degree for gateway branch detection
    out_degree: dict[str, int] = {}
    for nid in all_node_ids:
        out_degree[nid] = len(adj[nid])

    # Topological sort (Kahn's algorithm)
    in_deg: dict[str, int] = {}
    for nid in all_node_ids:
        in_deg[nid] = len(incoming[nid])

    queue: deque[str] = deque()
    for nid in all_node_ids:
        if in_deg[nid] == 0:
            queue.append(nid)

    topo_order: list[str] = []
    while queue:
        nid = queue.popleft()
        topo_order.append(nid)
        for tgt in adj[nid]:
            if tgt in all_node_ids:
                in_deg[tgt] -= 1
                if in_deg[tgt] == 0:
                    queue.append(tgt)

    # Defensive: if any node not in topo_order (e.g., cycle), append remaining
    for nid in all_node_ids:
        if nid not in topo_order:
            topo_order.append(nid)

    # --- Layer assignment ---
    # Layer 0 = first nodes in topological order
    layer_of: dict[str, int] = {}
    for nid in topo_order:
        layer_of[nid] = 0

    # Propagate layers: node layer = max(parent layers) + 1
    for nid in topo_order:
        current_layer = layer_of[nid]
        for tgt in adj[nid]:
            if tgt in all_node_ids:
                layer_of[tgt] = max(layer_of.get(tgt, 0), current_layer + 1)

    # Ensure all nodes have a layer (fallback for disconnected)
    max_layer = max(layer_of.values()) if layer_of else 0
    for nid in all_node_ids:
        if nid not in layer_of:
            layer_of[nid] = max_layer + 1
            max_layer += 1

    # Build layer -> list of node ids (preserve topological order within layer)
    layers: dict[int, list[str]] = defaultdict(list)
    for nid in topo_order:
        if nid in all_node_ids:
            layers[layer_of[nid]].append(nid)

    # Handle nodes in topo_order that may not have been in the original all_node_ids
    # (this shouldn't happen, but be safe)
    for nid in all_node_ids:
        if nid not in topo_order:
            lyr = layer_of.get(nid, 0)
            layers[lyr].append(nid)

    # --- Column assignment within each layer ---
    col_of: dict[str, int] = {}

    for lyr in sorted(layers.keys()):
        nodes_in_layer = layers[lyr]
        # Group: parents from previous layer can influence column ordering
        # Simple approach: assign columns in order
        for col_idx, nid in enumerate(nodes_in_layer):
            col_of[nid] = col_idx

    # --- Center coordinates ---
    positions: dict[str, dict] = {}

    for nid in all_node_ids:
        lyr = layer_of.get(nid, 0)
        col = col_of.get(nid, 0)
        w, h = _get_dim(bpmn_nodes[nid]["bpmn_type"])
        x = col * COL_SPACING + w // 2 + 50   # 50px left margin
        y = lyr * ROW_SPACING + h // 2 + 50   # 50px top margin

        entry: dict = {
            "x": x,
            "y": y,
            "layer": lyr,
            "col": col,
        }

        # Determine branch sides for gateways with out_degree > 1
        if "gateway" in bpmn_nodes[nid]["bpmn_type"] and out_degree.get(nid, 0) > 1:
            left_ids: list[str] = []
            right_ids: list[str] = []
            # Label-based heuristic: "no"/"否" edges go left, others right
            for edge in bpmn_edges:
                if edge["src"] == nid:
                    lbl = (edge.get("label") or "").lower()
                    if lbl in ("no", "n", "否", "false", "拒绝", "驳回"):
                        left_ids.append(edge["tgt"])
                    else:
                        right_ids.append(edge["tgt"])
                    # Also check condition
                    cond = (edge.get("condition") or "").lower()
                    if cond and not lbl:
                        if cond in ("no", "n", "否", "false"):
                            left_ids.append(edge["tgt"])
                        else:
                            right_ids.append(edge["tgt"])
            # If no left ones detected, keep them all right
            if left_ids:
                entry["left_branch_ids"] = left_ids
            if right_ids:
                entry["right_branch_ids"] = right_ids

        positions[nid] = entry

    return positions


def build_bpmn_xml(
    bpmn_nodes: dict,
    bpmn_edges: list,
    positions: dict,
    flow_name: str = "Flow",
    explanations: list | None = None,
) -> str:
    """
    Generate a complete BPMN 2.0 XML string from mapped elements and positions.

    Parameters:
        bpmn_nodes:    dict of id -> {id, bpmn_type, name, ...}
        bpmn_edges:    list of {id, src, tgt, label, condition}
        positions:     dict from auto_layout -> {x, y, layer, col, ...}
        flow_name:     name for the bpmn:process
        explanations:  optional list of mapping explanation strings

    Returns:
        BPMN 2.0 XML string
    """
    explanations = explanations or []

    # -----------------------------------------------------------------------
    # Root: bpmn:definitions
    # -----------------------------------------------------------------------
    definitions = ET.Element(f"{{{NS_BPMN}}}definitions")
    definitions.set("targetNamespace", "http://bpmn.io/schema/bpmn")
    definitions.set(f"{{{NS_MODELER}}}executionPlatform", "Camunda Cloud")
    definitions.set(f"{{{NS_MODELER}}}executionPlatformVersion", "1.0.0")

    # -----------------------------------------------------------------------
    # bpmn:process
    # -----------------------------------------------------------------------
    process = ET.SubElement(definitions, f"{{{NS_BPMN}}}process")
    process.set("id", f"Process_{flow_name.replace(' ', '_')}")
    process.set("name", flow_name)
    process.set("isExecutable", "true")

    # -----------------------------------------------------------------------
    # Add explanations as documentation if present
    # -----------------------------------------------------------------------
    if explanations:
        documentation = ET.SubElement(process, f"{{{NS_BPMN}}}documentation")
        documentation.set("id", f"Doc_{flow_name.replace(' ', '_')}")
        documentation.text = "\n".join(explanations)

    # -----------------------------------------------------------------------
    # Track incoming/outgoing edges for each node
    # -----------------------------------------------------------------------
    # incoming_map: tgt -> list of edge ids
    # outgoing_map: src -> list of edge ids
    incoming_map: dict[str, list[str]] = defaultdict(list)
    outgoing_map: dict[str, list[str]] = defaultdict(list)

    for edge in bpmn_edges:
        outgoing_map[edge["src"]].append(edge["id"])
        incoming_map[edge["tgt"]].append(edge["id"])

    # Collect all node IDs that actually appear in edges (to determine flow nodes)
    flow_node_ids: set[str] = set()
    for edge in bpmn_edges:
        flow_node_ids.add(edge["src"])
        flow_node_ids.add(edge["tgt"])
    # Also include any nodes from bpmn_nodes that are in positions
    for nid in bpmn_nodes:
        if nid in positions:
            flow_node_ids.add(nid)

    # Sort for deterministic output
    sorted_node_ids = sorted(flow_node_ids, key=lambda x: (
        positions.get(x, {}).get("layer", 0),
        positions.get(x, {}).get("col", 0),
        x,
    ))

    # -----------------------------------------------------------------------
    # Helper to create BPMN flow node elements
    # -----------------------------------------------------------------------
    _flow_node_elements: dict[str, ET.Element] = {}

    for nid in sorted_node_ids:
        if nid not in bpmn_nodes:
            continue
        node = bpmn_nodes[nid]
        bpmn_type = node["bpmn_type"]
        name = node.get("name", nid)

        # Skip subprocess subgraph nodes (not true BPMN flow nodes)
        if bpmn_type == "bpmn:subProcess" and node.get("mermaid_type") == "subgraph":
            continue

        elem = ET.SubElement(process, bpmn_type)
        elem.set("id", nid)
        if name:
            elem.set("name", name)

        # Add incoming/outgoing references
        for in_edge_id in incoming_map.get(nid, []):
            inc = ET.SubElement(elem, f"{{{NS_BPMN}}}incoming")
            inc.text = in_edge_id

        for out_edge_id in outgoing_map.get(nid, []):
            outg = ET.SubElement(elem, f"{{{NS_BPMN}}}outgoing")
            outg.text = out_edge_id

        _flow_node_elements[nid] = elem

    # -----------------------------------------------------------------------
    # bpmn:sequenceFlow elements
    # -----------------------------------------------------------------------
    _edge_elements: dict[str, ET.Element] = {}

    for edge in bpmn_edges:
        eid = edge["id"]
        src = edge["src"]
        tgt = edge["tgt"]
        label = edge.get("label", "")
        condition = edge.get("condition", "")

        seq = ET.SubElement(process, f"{{{NS_BPMN}}}sequenceFlow")
        seq.set("id", eid)
        seq.set("sourceRef", src)
        seq.set("targetRef", tgt)

        # If this edge originates from a gateway with a condition, add conditionExpression
        # Note: Part 1's condition field may be empty due to case-sensitive check,
        # so we also check if src is a gateway and label is non-empty
        src_node_info = bpmn_nodes.get(src)
        src_is_gateway = src_node_info and ("gateway" in src_node_info.get("bpmn_type", "").lower())

        if condition or (label and src_is_gateway):
            cond_text = condition if condition else label
            cond_elem = ET.SubElement(
                seq, f"{{{NS_BPMN}}}conditionExpression"
            )
            cond_elem.set(
                f"{{{NS_XSI}}}type", "tFormalExpression"
            )
            cond_elem.text = f"${{{cond_text}}}"
        elif label and not src_is_gateway:
            seq.set("name", label)

        _edge_elements[eid] = seq

    # -----------------------------------------------------------------------
    # bpmndi:BPMNDiagram
    # -----------------------------------------------------------------------
    diagram = ET.SubElement(definitions, f"{{{NS_BPMNDI}}}BPMNDiagram")
    diagram.set("id", f"BPMNDiagram_{flow_name.replace(' ', '_')}")

    plane = ET.SubElement(diagram, f"{{{NS_BPMNDI}}}BPMNPlane")
    plane.set("id", f"BPMNPlane_{flow_name.replace(' ', '_')}")
    plane.set("bpmnElement", process.get("id"))

    # -----------------------------------------------------------------------
    # BPMNShape for each node
    # -----------------------------------------------------------------------
    for nid in sorted_node_ids:
        if nid not in bpmn_nodes:
            continue
        node = bpmn_nodes[nid]
        bpmn_type = node["bpmn_type"]

        # Skip subgraph pseudo-nodes for shape rendering
        if bpmn_type == "bpmn:subProcess" and node.get("mermaid_type") == "subgraph":
            continue

        if nid not in positions:
            continue

        pos = positions[nid]
        cx = pos["x"]
        cy = pos["y"]
        w, h = _get_dim(bpmn_type)

        # Upper-left corner for dc:Bounds
        ulx = cx - w // 2
        uly = cy - h // 2

        shape = ET.SubElement(plane, f"{{{NS_BPMNDI}}}BPMNShape")
        shape.set("id", f"Shape_{nid}")
        shape.set("bpmnElement", nid)

        bounds = ET.SubElement(shape, f"{{{NS_DC}}}Bounds")
        bounds.set("x", str(ulx))
        bounds.set("y", str(uly))
        bounds.set("width", str(w))
        bounds.set("height", str(h))

        # BPMNLabel (required for bpmn.io compatibility)
        label_elt = ET.SubElement(shape, f"{{{NS_BPMNDI}}}BPMNLabel")

        # Label bounds positioned below the node
        label_bounds = ET.SubElement(label_elt, f"{{{NS_DC}}}Bounds")
        label_bounds.set("x", str(cx - 50))
        label_bounds.set("y", str(uly + h + 5))
        label_bounds.set("width", "100")
        label_bounds.set("height", "14")

    # -----------------------------------------------------------------------
    # BPMNEdge for each sequence flow
    # -----------------------------------------------------------------------
    for edge in bpmn_edges:
        eid = edge["id"]
        src = edge["src"]
        tgt = edge["tgt"]

        # Skip if src or tgt positions are missing
        if src not in positions or tgt not in positions:
            continue
        # Skip if src or tgt are subgraph markers
        if src not in bpmn_nodes or tgt not in bpmn_nodes:
            continue

        src_pos = positions[src]
        tgt_pos = positions[tgt]
        src_dim = _get_dim(bpmn_nodes[src]["bpmn_type"])
        tgt_dim = _get_dim(bpmn_nodes[tgt]["bpmn_type"])

        # Determine if this is a branch edge from a gateway
        src_node = bpmn_nodes[src]
        is_branch = "gateway" in src_node["bpmn_type"] and src_pos.get("right_branch_ids") or src_pos.get("left_branch_ids")
        branch_side = None
        if is_branch:
            right_ids = src_pos.get("right_branch_ids", [])
            left_ids = src_pos.get("left_branch_ids", [])
            if tgt in right_ids:
                branch_side = "right"
            elif tgt in left_ids:
                branch_side = "left"

        waypoints = calculate_waypoints(
            src_pos, tgt_pos, src_dim, tgt_dim,
            is_branch=bool(is_branch or branch_side or (src_pos.get("right_branch_ids") or src_pos.get("left_branch_ids"))),
            branch_side=branch_side,
        )

        bpmn_edge = ET.SubElement(plane, f"{{{NS_BPMNDI}}}BPMNEdge")
        bpmn_edge.set("id", f"Edge_{eid}")
        bpmn_edge.set("bpmnElement", eid)

        for wp in waypoints:
            wp_elem = ET.SubElement(bpmn_edge, f"{{{NS_DI}}}waypoint")
            wp_elem.set("x", str(wp[0]))
            wp_elem.set("y", str(wp[1]))

        # BPMNLabel for edge if it has a label
        if edge.get("label"):
            edge_label = ET.SubElement(bpmn_edge, f"{{{NS_BPMNDI}}}BPMNLabel")
            # Position label at midpoint of first and last waypoint
            if len(waypoints) >= 2:
                mid_x = (waypoints[0][0] + waypoints[-1][0]) // 2
                mid_y = (waypoints[0][1] + waypoints[-1][1]) // 2
                label_bounds = ET.SubElement(edge_label, f"{{{NS_DC}}}Bounds")
                label_bounds.set("x", str(mid_x - 50))
                label_bounds.set("y", str(mid_y - 7))
                label_bounds.set("width", "100")
                label_bounds.set("height", "14")
        else:
            # Empty label for compat
            ET.SubElement(bpmn_edge, f"{{{NS_BPMNDI}}}BPMNLabel")

    # -----------------------------------------------------------------------
    # Serialize to string
    # -----------------------------------------------------------------------
    raw_xml = ET.tostring(definitions, encoding="unicode")
    return format_bpmn_xml(raw_xml)


def format_bpmn_xml(xml_str: str) -> str:
    """
    Pretty-print XML string using xml.dom.minidom.

    Parameters:
        xml_str: raw XML string

    Returns:
        Indented, human-readable XML string without the XML declaration.
    """
    dom = minidom.parseString(xml_str.encode("utf-8"))
    pretty = dom.toprettyxml(indent="  ")
    # Remove the XML declaration line
    lines = pretty.splitlines()
    if lines and lines[0].startswith("<?xml"):
        lines = lines[1:]
    # Remove trailing empty lines
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def calculate_waypoints(
    src_pos: dict,
    tgt_pos: dict,
    src_dim: tuple[int, int],
    tgt_dim: tuple[int, int],
    is_branch: bool = False,
    branch_side: str | None = None,
) -> list[tuple[int, int]]:
    """
    Calculate di:waypoint coordinates for a BPMNEdge between two nodes.

    Parameters:
        src_pos:     source position dict {x, y, ...} (center coordinates)
        tgt_pos:     target position dict {x, y, ...} (center coordinates)
        src_dim:     (width, height) of source node
        tgt_dim:     (width, height) of target node
        is_branch:   if True, use L-shaped routing for gateway branches
        branch_side: "left" or "right" for branch direction

    Returns:
        list of (x, y) integer coordinate tuples
    """
    sx, sy = src_pos["x"], src_pos["y"]
    tx, ty = tgt_pos["x"], tgt_pos["y"]
    sw, sh = src_dim
    tw, th = tgt_dim

    # Source exits from its boundary, target enters at its boundary
    src_layer = src_pos.get("layer", 0)
    tgt_layer = tgt_pos.get("layer", 0)

    if is_branch and branch_side:
        # Branch routing: L-shaped with corner waypoint
        src_right = sx + sw // 2
        src_left = sx - sw // 2
        tgt_top = ty - th // 2
        tgt_bottom = ty + th // 2
        tgt_left = tx - tw // 2
        tgt_right = tx + tw // 2

        if branch_side == "right":
            # Exit from right side of source, go right then down/up to target
            corner_x = max(src_right + 40, tx + tw // 2 + 20)
            # First waypoint: source right edge center
            wp1 = (src_right, sy)
            # Corner waypoint
            wp2 = (corner_x, sy)
            # Last waypoint: enter target from left or top
            if tgt_layer > src_layer:
                # Target is below: enter from top
                wp3 = (tx, tgt_top)
            else:
                # Same row or above: enter from left
                wp3 = (tgt_left, ty)
            return [wp1, wp2, wp3]
        else:
            # branch_side == "left": exit from left side of source
            corner_x = min(src_left - 40, tx - tw // 2 - 20)
            wp1 = (src_left, sy)
            wp2 = (corner_x, sy)
            if tgt_layer > src_layer:
                wp3 = (tx, tgt_top)
            else:
                wp3 = (tgt_right, ty)
            return [wp1, wp2, wp3]

    # Default vertical routing: top-to-bottom
    if tgt_layer > src_layer:
        # Source is above target
        src_bottom = sy + sh // 2
        tgt_top = ty - th // 2
        return [(sx, src_bottom), (tx, tgt_top)]

    # Target is above source (reverse flow)
    if tgt_layer < src_layer:
        src_top = sy - sh // 2
        tgt_bottom = ty + th // 2
        return [(sx, src_top), (tx, tgt_bottom)]

    # Same layer: horizontal routing
    if tx > sx:
        # Target is to the right
        src_right = sx + sw // 2
        tgt_left = tx - tw // 2
        return [(src_right, sy), (tgt_left, ty)]
    else:
        # Target is to the left
        src_left = sx - sw // 2
        tgt_right = tx + tw // 2
        return [(src_left, sy), (tgt_right, ty)]


# ---------------------------------------------------------------------------
# Quick self-test when run directly
# ---------------------------------------------------------------------------

def _demo() -> None:
    """Run a quick self-test with a simple flow."""
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mermaid_to_bpmn_part1 import parse_mermaid, auto_map_elements, get_element_dimensions

    # Use a simple Mermaid flow that Part 1 parses reliably.
    # Explicitly declare all nodes first so Part 1 picks them up.
    sample = """---
title: Approval Flow
---
graph TD
    Start([Start])
    Review[Review Application]
    Approve([End])
    Decision{Check?}
    Start -->|Submit| Review
    Review -->|Approve| Approve
    Review -->|Reject| Decision
    Decision -->|Yes| Approve
    Decision -->|No| Review"""

    nodes, edges, name = parse_mermaid(sample)
    print("Nodes:", nodes)
    print("Edges:", edges)

    bpmn_nodes, bpmn_edges, explanations = auto_map_elements(nodes, edges)
    print("\nBPMN Nodes:")
    for nid, ndata in sorted(bpmn_nodes.items()):
        print(f"  {nid}: {ndata['bpmn_type']} name={ndata['name']!r}")

    positions = auto_layout(bpmn_nodes, bpmn_edges)

    print("\n=== Positions ===")
    for nid, pos in sorted(positions.items()):
        print(f"  {nid}: x={pos['x']}, y={pos['y']}, layer={pos['layer']}, col={pos['col']}")

    xml_out = build_bpmn_xml(bpmn_nodes, bpmn_edges, positions, name, explanations)
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_output.bpmn")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml_out)
    print(f"\n=== XML written to {out_path} ===")
    print(xml_out[:1000] + "...")


if __name__ == "__main__":
    _demo()
