"""
mermaid_to_bpmn.py --- Convert Mermaid flowchart syntax to BPMN 2.0 XML
Uses only Python stdlib.
"""

import sys
import re
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
from collections import defaultdict, deque
import uuid

"""
mermaid_to_bpmn_part1.py --- Mermaid Parser + BPMN Element Mapping Module (Part 1 of 2)

Parses Mermaid flowchart syntax into a structured node/edge representation, then
intelligently maps those elements to BPMN 2.0 types based on shape, connectivity,
and keyword heuristics. Provides dimension lookup for BPMN element layout.

This module uses only Python stdlib (re, json).
"""



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _extract_inline_nodes(line: str, nodes: dict) -> str:
    """
    Extract inline node definitions from a line that may also contain an edge.
    Returns the 'edge-only' version with node definitions replaced by their IDs.

    Handles:
      A[name] --> B{name}    ->  A --> B
      A --> B[name]          ->  A --> B
      A[name] --> B          ->  A --> B
      A -- label --> B[name] ->  A -- label --> B
    """
    pat = re.compile(r"(\w[\w\d_]*)\[([^\]]+)\]|(\w[\w\d_]*)\{([^}]+)\}")
    result = line
    offset = 0
    for match in pat.finditer(line):
        nid = match.group(1) or match.group(3)
        name = match.group(2) or match.group(4)
        shape_char = match.group(0)[len(nid)]
        nodes[nid] = {
            "id": nid,
            "name": name.strip(),
            "type": "rectangle" if shape_char == '[' else "rhombus"
        }
        # Replace the full match with just the ID, adjusting for previous replacements
        start = match.start() + offset
        end = match.end() + offset
        result = result[:start] + nid + result[end:]
        offset += len(nid) - (end - start)
    return result


def parse_mermaid(text: str) -> tuple[dict, list, str]:
    """
    Parse a Mermaid flowchart string into structured nodes, edges, and flow name.

    Supports:
      - Rectangle nodes:   A[text]
      - Stadium nodes:     A([text])  or  A((text))
      - Rhombus nodes:     A{text}
      - Subprocess nodes:  A[[text]]
      - Edges: A --> B, A -->|label| B, A -.-> B, A ==> B
      - Inline node defs:  A[name] --> B{name}  (nodes defined inline in edge lines)
      - Subgraph blocks (for lane detection)
      - Direction markers: LR / TD (or TB / BT / RL)

    Returns:
        nodes (dict):      Mapping of node ID -> {id, name, type}
        edges (list):      List of dicts {src, tgt, label}
        name (str):        Flow name extracted from graph title (or "Flow" default)
    """
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    name = "Flow"

    # Clean and normalise line endings
    lines = text.replace("\r\n", "\n").split("\n")

    # Track subgraph boundaries (for lane detection later)
    subgraph_stack: list[str] = []
    in_subgraph = False
    current_subgraph_name = ""

    # Collect all edge-like patterns first so we can compute degrees later
    raw_edges: list[tuple[str, str, str]] = []  # (src, tgt, label)

    # Stage 1: Identify direction and title
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Graph title in --- block:  ---  title: ...  ---
        if stripped == "---":
            continue
        if re.match(r"^\s*title\s*:", stripped, re.IGNORECASE):
            m = re.match(r"^\s*title\s*:\s*(.+)$", stripped, re.IGNORECASE)
            if m:
                name = m.group(1).strip().strip('"').strip("'")
            continue
        if re.match(r"^\s*(?:graph|flowchart)\s+(TB|TD|BT|RL|LR)", stripped, re.IGNORECASE):
            m = re.match(r"^\s*(?:graph|flowchart)\s+(TB|TD|BT|RL|LR)\s+(.+)$", stripped, re.IGNORECASE)
            if m:
                name = m.group(2).strip().strip('"').strip("'")
            continue
        if re.match(r"^\s*(?:graph|flowchart)\s+(TB|TD|BT|RL|LR)\s*$", stripped, re.IGNORECASE):
            continue

    # Stage 2: Parse nodes and edges
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Skip graph/flowchart direction declaration and title lines
        if re.match(r"^\s*(?:graph|flowchart)\s+(TB|TD|BT|RL|LR)", stripped, re.IGNORECASE):
            continue
        if re.match(r"^\s*title\s*:", stripped, re.IGNORECASE):
            continue
        if stripped == "---":
            continue

        # Subgraph start
        sg_match = re.match(r"^\s*subgraph\s+(.+)$", stripped)
        if sg_match:
            subgraph_name = sg_match.group(1).strip().strip('"').strip("'")
            subgraph_stack.append(subgraph_name)
            in_subgraph = True
            current_subgraph_name = subgraph_name
            # Register subgraph as a pseudo-node for lane tracking
            safe_name = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff_]', '_', subgraph_name)[:30]
            sg_id = "__sg_" + safe_name + "__"
            if sg_id in nodes:
                sg_id = "__sg_" + safe_name + "_" + str(len(subgraph_stack)) + "__"
            nodes[sg_id] = {
                "id": sg_id,
                "name": subgraph_name,
                "type": "subgraph"
            }
            continue

        # Subgraph end
        if stripped == "end" and subgraph_stack:
            subgraph_stack.pop()
            in_subgraph = bool(subgraph_stack)
            current_subgraph_name = subgraph_stack[-1] if subgraph_stack else ""
            continue

        # Extract inline node definitions FIRST (e.g., A[name] --> B{name})
        edge_line = _extract_inline_nodes(stripped, nodes)

        # Now edge_line is in format: A --> B  or  A -- label --> B  or A -->|label| B
        # --- Edge detection ---

        # Pattern with label via pipe:  A -->|label| B
        edge_label = re.match(
            r"^\s*(\w[\w\d_]*)\s*"
            r"[-\\.=]*(?:>|\))\s*"
            r"\|([^|]*)\|\s*"
            r"(\w[\w\d_]*)\s*$",
            edge_line
        )
        if edge_label:
            src, label, tgt = edge_label.groups()
            raw_edges.append((src, tgt, label.strip()))
            continue

        # Pattern with inline label:  A -- label --> B
        edge_inline = re.match(
            r"^\s*(\w[\w\d_]*)\s+"
            r"--?[-.]*"
            r"\s+(.+?)\s+"
            r"-?[-.]*>+\s*"
            r"(\w[\w\d_]*)\s*$",
            edge_line
        )
        if edge_inline:
            src, label, tgt = edge_inline.groups()
            raw_edges.append((src, tgt, label.strip()))
            continue

        # Pattern without label:  A --> B
        edge_nolabel = re.match(
            r"^\s*(\w[\w\d_]*)\s*"
            r"(?:-->|==>|-.->|->|=>|-->|---?)\s*"
            r"(\w[\w\d_]*)\s*$",
            edge_line
        )
        if edge_nolabel:
            src, tgt = edge_nolabel.groups()
            raw_edges.append((src, tgt, ""))
            continue

        # --- Standalone node detection (no edge on this line) ---
        # Subprocess: A[[name]]
        sp_match = re.match(r"^\s*(\w[\w\d_]*)\[\[([^\]]+)\]\]\s*$", stripped)
        if sp_match:
            nid, nname = sp_match.groups()
            nodes[nid] = {"id": nid, "name": nname.strip(), "type": "subprocess"}
            continue

        # Stadium (double parens): A((name))
        st_match = re.match(r"^\s*(\w[\w\d_]*)\(\(([^)]+)\)\)\s*$", stripped)
        if st_match:
            nid, nname = st_match.groups()
            raw_name = nname.strip()
            raw_name = raw_name.strip("[]")
            nodes[nid] = {"id": nid, "name": raw_name, "type": "stadium"}
            continue

        # Stadium (single parens): A(name)
        st_single = re.match(r"^\s*(\w[\w\d_]*)\(([^()]+)\)\s*$", stripped)
        if st_single:
            nid, nname = st_single.groups()
            raw_name = nname.strip()
            raw_name = raw_name.strip("[]")
            nodes[nid] = {"id": nid, "name": raw_name, "type": "stadium"}
            continue

        # Rhombus: A{name} (standalone, no edge)
        rh_match = re.match(r"^\s*(\w[\w\d_]*)\{([^}]+)\}\s*$", stripped)
        if rh_match:
            nid, nname = rh_match.groups()
            nodes[nid] = {"id": nid, "name": nname.strip(), "type": "rhombus"}
            continue

        # Rectangle (plain): A[name] (standalone, no edge)
        re_match = re.match(r"^\s*(\w[\w\d_]*)\[([^\]]+)\]\s*$", stripped)
        if re_match:
            nid, nname = re_match.groups()
            nodes[nid] = {"id": nid, "name": nname.strip(), "type": "rectangle"}
            continue

        # Fallback: any single word left could be a node reference (already registered)
        # or a loose comment --- ignore.

    # Stage 3: Build final edges list from raw edges
    for src, tgt, label in raw_edges:
        edges.append({
            "src": src,
            "tgt": tgt,
            "label": label
        })

    return nodes, edges, name


def auto_map_elements(nodes: dict, edges: list) -> tuple[dict, list, list]:
    """
    Map parsed Mermaid elements to BPMN 2.0 types using smart heuristics.

    Rules:
      a) Stadium with in_degree == 0  -> startEvent
      b) Stadium with out_degree == 0 -> endEvent
      c) Rhombus -> gateway (exclusive/parallel/inclusive based on labels)
      d) Rectangle -> task subtype based on name keywords
      e) Subprocess -> bpmn:subProcess
      f) Subgraph tracked for lanes (noted in explanations)

    Returns:
        bpmn_nodes (dict):    id -> {id, bpmn_type, name, mermaid_type, mapping_reason}
        bpmn_edges (list):    [ {id, src, tgt, label, condition}, ... ]
        explanations (list):  Human-readable mapping explanation strings
    """
    bpmn_nodes: dict[str, dict] = {}
    explanations: list[str] = []

    # Compute in/out degree for each node from the edge list
    in_degree: dict[str, int] = {}
    out_degree: dict[str, int] = {}
    outgoing_edges: dict[str, list[dict]] = {}

    for node_id in nodes:
        in_degree[node_id] = 0
        out_degree[node_id] = 0
        outgoing_edges[node_id] = []

    for edge in edges:
        src = edge["src"]
        tgt = edge["tgt"]
        if src in out_degree:
            out_degree[src] += 1
            outgoing_edges[src].append(edge)
        if tgt in in_degree:
            in_degree[tgt] += 1
        # Register nodes referenced in edges but not yet in nodes dict
        if src not in nodes:
            nodes[src] = {"id": src, "name": src, "type": "rectangle"}
        if tgt not in nodes:
            nodes[tgt] = {"id": tgt, "name": tgt, "type": "rectangle"}

    # Map each node
    for node_id, node in nodes.items():
        mtype = node.get("type", "rectangle")
        mname = node.get("name", node_id)

        # Skip subgraph pseudo-nodes (handled via notes)
        if mtype == "subgraph":
            bpmn_nodes[node_id] = {
                "id": node_id,
                "bpmn_type": "bpmn:subProcess",
                "name": mname,
                "mermaid_type": "subgraph",
                "mapping_reason": "subgraph block, for lane partitioning"
            }
            msg = '"' + chr(9632) + ' ' + mname + '" -> lane container (subProcess marker, handled by Part 2 as lane)'
            explanations.append(msg)
            continue

        bpmn_type = "bpmn:task"  # default fallback
        reason = "default mapping to task"

        if mtype == "stadium":
            indeg = in_degree.get(node_id, 0)
            outdeg = out_degree.get(node_id, 0)
            if indeg == 0:
                bpmn_type = "bpmn:startEvent"
                reason = "in-degree " + str(indeg) + ", inferred as start event"
                msg = '"' + chr(9675) + ' ' + mname + '" -> startEvent (in-degree ' + str(indeg) + ', flow start)'
                explanations.append(msg)
            elif outdeg == 0:
                bpmn_type = "bpmn:endEvent"
                reason = "out-degree " + str(outdeg) + ", inferred as end event"
                msg = '"' + chr(9675) + ' ' + mname + '" -> endEvent (out-degree ' + str(outdeg) + ', flow end)'
                explanations.append(msg)
            else:
                # Stadium with both in and out edges --- treat as intermediate event
                bpmn_type = "bpmn:intermediateCatchEvent"
                reason = "stadium node with both inbound and outbound edges, treated as intermediate event"
                msg = '"' + chr(9675) + ' ' + mname + '" -> intermediateCatchEvent (stadium with both in/out)'
                explanations.append(msg)

        elif mtype == "rhombus":
            out_edges = outgoing_edges.get(node_id, [])
            bpmn_type, gw_reason = determine_gateway_subtype(node, out_edges)
            reason = gw_reason
            short_type = bpmn_type.split(":")[-1]
            msg = '"' + chr(9670) + ' ' + mname + '" -> ' + short_type + ' (' + gw_reason + ')'
            explanations.append(msg)

        elif mtype == "rectangle":
            bpmn_type, reason = _map_rectangle_task(mname)
            short_type = bpmn_type.split(":")[-1]
            msg = '"' + chr(9632) + ' ' + mname + '" -> ' + short_type + ' (' + reason + ')'
            explanations.append(msg)

        elif mtype == "subprocess":
            bpmn_type = "bpmn:subProcess"
            reason = "Mermaid [[ ]] syntax indicates subprocess"
            msg = '"' + chr(9632) + ' ' + mname + '" -> subProcess (Mermaid [[ ]] syntax)'
            explanations.append(msg)

        else:
            bpmn_type = "bpmn:task"
            reason = "unknown type " + str(mtype) + ", default mapping to task"

        bpmn_nodes[node_id] = {
            "id": node_id,
            "bpmn_type": bpmn_type,
            "name": mname,
            "mermaid_type": mtype,
            "mapping_reason": reason
        }

    # Map edges
    bpmn_edges: list[dict] = []
    for idx, edge in enumerate(edges):
        src = edge["src"]
        tgt = edge["tgt"]
        label = edge.get("label", "")

        # Determine if this edge should carry a condition expression
        condition = ""
        src_node = bpmn_nodes.get(src)
        if src_node and "gateway" in src_node["bpmn_type"]:
            if label:
                condition = label

        bpmn_edges.append({
            "id": "edge_" + str(idx + 1),
            "src": src,
            "tgt": tgt,
            "label": label,
            "condition": condition
        })

    return bpmn_nodes, bpmn_edges, explanations


def get_element_dimensions(bpmn_type: str) -> tuple[int, int]:
    """
    Return (width, height) in layout units for a given BPMN element type.

    These are standard BPMN 2.0 visual dimensions used for diagram layout.
    """
    dims: dict[str, tuple[int, int]] = {
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
    return dims.get(bpmn_type, (100, 80))


def determine_gateway_subtype(node: dict, out_edges: list) -> tuple[str, str]:
    """
    Determine the BPMN gateway subtype from a rhombus (decision) node.

    Heuristics (checked in order):
      1. Any outbound edge label contains "||"               -> parallelGateway
      2. Any outbound edge label contains inclusive keywords -> inclusiveGateway
      3. Out-degree > 1 AND any label has exclusive keywords -> exclusiveGateway
      4. Else -> exclusiveGateway (default)

    Exclusive keywords (Chinese/English): shi/fou/tongguo/jujue/tongyi/bo...
    Inclusive keywords: douyao/tongshi/dou/suoyou

    Returns:
        (bpmn_type_str, explanation_str)
    """
    outdeg = len(out_edges)
    labels = [e.get("label", "") for e in out_edges]

    # Check for parallel marker
    for label in labels:
        if "||" in label:
            return ("bpmn:parallelGateway", "edge label contains || marker, inferred as parallel gateway")

    # Check for inclusive keywords
    inclusive_kw = ["\u90fd\u8981", "\u540c\u65f6", "\u90fd", "\u6240\u6709", "\u4efb\u610f"]
    for label in labels:
        for kw in inclusive_kw:
            if kw in label:
                return (
                    "bpmn:inclusiveGateway",
                    'edge label contains "' + kw + '", inferred as inclusive gateway'
                )

    # Check for exclusive keywords
    exclusive_kw = [
        "\u662f", "\u5426", "\u901a\u8fc7", "\u62d2\u7edd",
        "\u540c\u610f", "\u9a73\u56de", "Y", "N", "yes", "no"
    ]
    has_exclusive = False
    for label in labels:
        for kw in exclusive_kw:
            if kw in label:
                has_exclusive = True
                break
        if has_exclusive:
            break

    if outdeg > 1 and has_exclusive:
        return (
            "bpmn:exclusiveGateway",
            "out-degree " + str(outdeg) + " with exclusive keywords on edges, inferred as XOR gateway"
        )
    elif outdeg > 1 and not has_exclusive:
        return (
            "bpmn:exclusiveGateway",
            "out-degree " + str(outdeg) + " but edges lack labels, default XOR gateway"
        )
    else:
        return (
            "bpmn:exclusiveGateway",
            "diamond node default mapped to exclusive gateway (XOR)"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _map_rectangle_task(name: str) -> tuple[str, str]:
    """
    Map a rectangle Mermaid node to a BPMN task subtype based on name keywords.

    Keyword checks (case-insensitive, in priority order):
      - review/approve/submit/human related   -> userTask
      - system/auto/service/related           -> serviceTask
      - send/notify/email                     -> sendTask
      - receive/wait                          -> receiveTask
      - rule/validate                         -> businessRuleTask
      - script                                -> scriptTask
      - Default                               -> task (plain)

    Returns:
        (bpmn_type_str, reason_str)
    """
    lower = name.lower()

    # serviceTask (check BEFORE userTask — "系统" and "自动" take priority over generic keywords)
    service_kw = ["\u7cfb\u7edf", "\u81ea\u52a8", "service", "auto",
                  "\u8ba1\u7b97"]
    for kw in service_kw:
        if kw in lower:
            return ("bpmn:serviceTask", 'contains keyword "' + kw + '", inferred as service task')

    # userTask
    user_kw = ["\u5ba1", "\u5ba1\u6838", "\u5ba1\u6279", "review", "approve",
               "\u4eba\u5de5", "\u586b\u5199", "\u63d0\u4ea4",
               "\u53d1\u8d27", "\u4f9b\u5e94\u5546", "\u626b\u7801", "\u6536\u8d27",
               "\u5165\u5e93", "\u51fa\u5e93"]
    for kw in user_kw:
        if kw in lower:
            return ("bpmn:userTask", 'contains keyword "' + kw + '", inferred as user task')

    # sendTask
    send_kw = ["\u53d1\u9001", "\u901a\u77e5", "notify", "send", "email", "\u90ae\u4ef6"]
    for kw in send_kw:
        if kw in lower:
            return ("bpmn:sendTask", 'contains keyword "' + kw + '", inferred as send task')

    # receiveTask
    receive_kw = ["\u63a5\u6536", "\u7b49\u5f85", "receive", "wait", "\u6536\u53d6"]
    for kw in receive_kw:
        if kw in lower:
            return ("bpmn:receiveTask", 'contains keyword "' + kw + '", inferred as receive/wait task')

    # businessRuleTask
    rule_kw = ["\u89c4\u5219", "\u6821\u9a8c", "rule", "validate",
               "\u9a8c\u8bc1", "\u68c0\u67e5", "\u5ba1\u6838\u89c4\u5219"]
    for kw in rule_kw:
        if kw in lower:
            return ("bpmn:businessRuleTask", 'contains keyword "' + kw + '", inferred as business rule task')

    # scriptTask
    script_kw = ["\u811a\u672c", "script", "\u6267\u884c\u811a\u672c"]
    for kw in script_kw:
        if kw in lower:
            return ("bpmn:scriptTask", 'contains keyword "' + kw + '", inferred as script task')

    return ("bpmn:task", "no matching keywords, default plain task")


# ---------------------------------------------------------------------------
# Quick self-test when run directly
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Namespace constants
# ---------------------------------------------------------------------------

NS_BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
NS_BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
NS_DC = "http://www.omg.org/spec/DD/20100524/DC"
NS_DI = "http://www.omg.org/spec/DD/20100524/DI"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
NS_MODELER = "http://camunda.org/schema/Modeler/1.0"
NS_CAMUNDA = "http://camunda.org/schema/1.0/bpmn"
NS_ZEEBE = "http://camunda.org/schema/zeebe/1.0"

# Register namespaces so ElementTree uses the correct prefixes
ET.register_namespace("bpmn", NS_BPMN)
ET.register_namespace("bpmndi", NS_BPMNDI)
ET.register_namespace("dc", NS_DC)
ET.register_namespace("di", NS_DI)
ET.register_namespace("xsi", NS_XSI)
ET.register_namespace("modeler", NS_MODELER)
ET.register_namespace("camunda", NS_CAMUNDA)
ET.register_namespace("zeebe", NS_ZEEBE)

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
    # Ensure flow has a start event (Camunda requirement)
    # -----------------------------------------------------------------------
    has_start_event = any(
        "startEvent" in bpmn_nodes.get(nid, {}).get("bpmn_type", "")
        for nid in flow_node_ids if nid in bpmn_nodes
    )
    if not has_start_event and sorted_node_ids:
        # Find the earliest node (first in topological order) and add start event before it
        first_node = sorted_node_ids[0]
        start_id = "StartEvent_auto"
        first_pos = positions.get(first_node, {"x": 50, "y": 50, "layer": 0})
        # Place start event above the first node
        start_y = first_pos["y"] - 100
        if start_y < 50:
            start_y = 50
        start_pos = {"x": first_pos["x"], "y": start_y, "layer": 0, "col": 0}

        # Add to bpmn_nodes
        bpmn_nodes[start_id] = {
            "id": start_id,
            "bpmn_type": "bpmn:startEvent",
            "name": "开始",
            "mermaid_type": "stadium",
            "mapping_reason": "auto-added start event"
        }
        positions[start_id] = start_pos

        # Add edge from start to first node
        first_edge = f"Flow_start_{first_node}"
        bpmn_edges.insert(0, {
            "id": first_edge,
            "src": start_id,
            "tgt": first_node,
            "label": "",
            "condition": ""
        })
        incoming_map[first_node].append(first_edge)
        outgoing_map[start_id].append(first_edge)
        flow_node_ids.add(start_id)
        sorted_node_ids.insert(0, start_id)
        explanations.insert(0, "▶ 自动添加 startEvent（流程起始标记）")

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

        # Camunda 8 (Zeebe) extensions for task types
        bpmn_short = bpmn_type.split(":")[-1] if ":" in bpmn_type else bpmn_type
        if bpmn_short == "serviceTask":
            ext = ET.SubElement(elem, f"{{{NS_BPMN}}}extensionElements")
            td = ET.SubElement(ext, f"{{{NS_ZEEBE}}}taskDefinition")
            td.set("type", name.replace(" ", "_"))
        elif bpmn_short == "userTask":
            ext = ET.SubElement(elem, f"{{{NS_BPMN}}}extensionElements")
            fd = ET.SubElement(ext, f"{{{NS_ZEEBE}}}formDefinition")
            fd.set("formKey", "embedded:app:forms/" + name.replace(" ", "_") + ".html")
        elif bpmn_short == "sendTask":
            # Camunda 8 (Zeebe) supports sendTask only from 1.1+, and even then
            # it behaves identically to serviceTask. Map to serviceTask for
            # maximum compatibility.
            bpmn_nodes[nid]["bpmn_type"] = "bpmn:serviceTask"
            elem.tag = f"{{{NS_BPMN}}}serviceTask"
            ext = ET.SubElement(elem, f"{{{NS_BPMN}}}extensionElements")
            td = ET.SubElement(ext, f"{{{NS_ZEEBE}}}taskDefinition")
            td.set("type", name.replace(" ", "_") + "_send")
        elif bpmn_short == "task":
            # Camunda 8 doesn't support plain Undefined Task; map to serviceTask
            bpmn_nodes[nid]["bpmn_type"] = "bpmn:serviceTask"
            elem.tag = f"{{{NS_BPMN}}}serviceTask"
            ext = ET.SubElement(elem, f"{{{NS_BPMN}}}extensionElements")
            td = ET.SubElement(ext, f"{{{NS_ZEEBE}}}taskDefinition")
            td.set("type", name.replace(" ", "_") + "_task")

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
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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





def validate_bpmn_xml(xml_str, bpmn_nodes, bpmn_edges):
    """Validate BPMN 2.0 XML structure."""
    issues = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        issues.append("XML parse error: " + str(e))
        return issues
    bpmn_ns = "http://www.omg.org/spec/BPMN/20100524/MODEL"
    if bpmn_ns not in xml_str:
        issues.append("Missing BPMN namespace")
    process = None
    for elem in root.iter():
        if "process" in elem.tag and "}" in elem.tag:
            process = elem
            break
    if process is None:
        issues.append("Missing bpmn:process element")
    if process is not None:
        for child in process:
            tag = child.tag
            if "sequenceFlow" in tag or "lane" in tag or "documentation" in tag:
                continue
            nid = child.get("id", "")
            inc = sum(1 for c in child if "incoming" in c.tag)
            out = sum(1 for c in child if "outgoing" in c.tag)
            ts = tag.split("}")[-1] if "}" in tag else tag
            if "startEvent" not in ts and inc == 0:
                issues.append("Warning: node " + nid + " (" + ts + ") no incoming")
            if "endEvent" not in ts and out == 0:
                issues.append("Warning: node " + nid + " (" + ts + ") no outgoing")
    all_nids = set(bpmn_nodes.keys())
    for edge in bpmn_edges:
        if edge["src"] not in all_nids:
            issues.append("Error: seqFlow " + edge["id"] + " srcRef=" + edge["src"] + " not found")
        if edge["tgt"] not in all_nids:
            issues.append("Error: seqFlow " + edge["id"] + " tgtRef=" + edge["tgt"] + " not found")
    for elem in root.iter():
        if "BPMNEdge" in elem.tag:
            wp = sum(1 for w in elem.iter() if "waypoint" in w.tag)
            if wp < 2:
                issues.append("Warning: edge " + elem.get("id","") + " has " + str(wp) + " waypoints")
        if "BPMNShape" in elem.tag:
            b = elem.find("{http://www.omg.org/spec/DD/20100524/DC}Bounds")
            if b is not None:
                w = b.get("width", "0")
                h = b.get("height", "0")
                try:
                    if float(w) <= 0 or float(h) <= 0:
                        issues.append("Warning: shape " + elem.get("id","") + " zero dims")
                except ValueError:
                    pass
    return issues


def main():
    args = list(sys.argv)
    flags = {"format": "--format" in args, "validate": "--validate" in args}
    clean = [a for a in args if not a.startswith("--")]
    if len(clean) < 3:
        text = sys.stdin.read()
        out_path = clean[1] if len(clean) > 1 else "output.bpmn"
    else:
        text = clean[1]
        out_path = clean[2]
    try:
        nodes, edges, name = parse_mermaid(text)
        print("  [1/5] Parsed " + str(len(nodes)) + " nodes, " + str(len(edges)) + " edges", file=sys.stderr)
        bn, be, expls = auto_map_elements(nodes, edges)
        print("  [2/5] Mapped " + str(len(bn)) + " BPMN elements", file=sys.stderr)
        pos = auto_layout(bn, be)
        print("  [3/5] Layout for " + str(len(pos)) + " elements", file=sys.stderr)
        xml = build_bpmn_xml(bn, be, pos, name, expls)
        print("  [4/5] Generated BPMN 2.0 XML", file=sys.stderr)
        if flags["format"]:
            xml = format_bpmn_xml(xml)
        if flags["validate"]:
            issues = validate_bpmn_xml(xml, bn, be)
            if issues:
                print("  [5/5] Validation: " + str(len(issues)) + " issues", file=sys.stderr)
                for iss in issues:
                    print("    " + iss, file=sys.stderr)
            else:
                print("  [5/5] Validation passed", file=sys.stderr)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(xml)
        print("  Written to " + out_path, file=sys.stderr)
        if expls:
            print("\nExplanations:", file=sys.stderr)
            for e in expls:
                print("  " + e, file=sys.stderr)
        print(out_path)
    except Exception as e:
        print("Error: " + str(e), file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
