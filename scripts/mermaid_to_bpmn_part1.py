"""
mermaid_to_bpmn_part1.py --- Mermaid Parser + BPMN Element Mapping Module (Part 1 of 2)

Parses Mermaid flowchart syntax into a structured node/edge representation, then
intelligently maps those elements to BPMN 2.0 types based on shape, connectivity,
and keyword heuristics. Provides dimension lookup for BPMN element layout.

This module uses only Python stdlib (re, json).
"""

import re


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_mermaid(text: str) -> tuple[dict, list, str]:
    """
    Parse a Mermaid flowchart string into structured nodes, edges, and flow name.

    Supports:
      - Rectangle nodes:   A[text]
      - Stadium nodes:     A([text])  or  A((text))
      - Rhombus nodes:     A{text}
      - Subprocess nodes:  A[[text]]
      - Edges: A --> B, A -->|label| B, A -.-> B, A ==> B
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
        if re.match(r"^\s*graph\s+(TB|TD|BT|RL|LR)", stripped, re.IGNORECASE):
            m = re.match(r"^\s*graph\s+(TB|TD|BT|RL|LR)\s+(.+)$", stripped, re.IGNORECASE)
            if m:
                name = m.group(2).strip().strip('"').strip("'")
            continue
        if re.match(r"^\s*graph\s+(TB|TD|BT|RL|LR)\s*$", stripped, re.IGNORECASE):
            continue

    # Stage 2: Parse nodes and edges
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Skip graph direction declaration and title lines
        if re.match(r"^\s*graph\s+(TB|TD|BT|RL|LR)", stripped, re.IGNORECASE):
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
            # Use a unique ID based on stack depth plus slugified name
            safe_name = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff_]', '_', subgraph_name)[:30]
            sg_id = "__sg_" + safe_name + "__"
            # Ensure uniqueness by appending counter if needed
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

        # --- Edge detection ---
        # Pattern with label:  A -->|label| B  or  A ==>|label| B  or  A -.->|label| B
        # NOTE: Mermaid uses A -->|label| B (no second arrow after the label)
        edge_label = re.match(
            r"^\s*(\w[\w\d_]*)\s*"
            r"[-\\.=]*(?:>|\))\s*"
            r"\|([^|]*)\|\s*"
            r"(\w[\w\d_]*)\s*$",
            stripped
        )
        if edge_label:
            src, label, tgt = edge_label.groups()
            raw_edges.append((src, tgt, label.strip()))
            continue

        # Pattern without label:  A --> B  or  A ==> B  or  A -.-> B
        edge_nolabel = re.match(
            r"^\s*(\w[\w\d_]*)\s*"
            r"(?:-->|==>|-\.->|->|=>)\s*"
            r"(\w[\w\d_]*)\s*$",
            stripped
        )
        if edge_nolabel:
            src, tgt = edge_nolabel.groups()
            raw_edges.append((src, tgt, ""))
            continue

        # --- Node detection ---
        # Subprocess: A[[name]]
        sp_match = re.match(r"^\s*(\w[\w\d_]*)\[\[([^\]]+)\]\]\s*$", stripped)
        if sp_match:
            nid, nname = sp_match.groups()
            nodes[nid] = {"id": nid, "name": nname.strip(), "type": "subprocess"}
            continue

        # Stadium (double parens): A((name)) --- common in graph TD
        st_match = re.match(r"^\s*(\w[\w\d_]*)\(\(([^)]+)\)\)\s*$", stripped)
        if st_match:
            nid, nname = st_match.groups()
            raw_name = nname.strip()
            # Strip surrounding brackets if present: ((text)) -> text, (([text])) -> text
            raw_name = raw_name.strip("[]")
            nodes[nid] = {"id": nid, "name": raw_name, "type": "stadium"}
            continue

        # Stadium (single parens): A(name) --- also used for start/end
        # Only match if there is content between parens (no nested parens)
        st_single = re.match(r"^\s*(\w[\w\d_]*)\(([^()]+)\)\s*$", stripped)
        if st_single:
            nid, nname = st_single.groups()
            raw_name = nname.strip()
            # Strip surrounding brackets if present: (text) -> text, ([text]) -> text
            raw_name = raw_name.strip("[]")
            nodes[nid] = {"id": nid, "name": raw_name, "type": "stadium"}
            continue

        # Rhombus: A{name}
        rh_match = re.match(r"^\s*(\w[\w\d_]*)\{([^}]+)\}\s*$", stripped)
        if rh_match:
            nid, nname = rh_match.groups()
            nodes[nid] = {"id": nid, "name": nname.strip(), "type": "rhombus"}
            continue

        # Rectangle (plain): A[name]
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

    # userTask
    user_kw = ["\u5ba1", "\u5ba1\u6838", "\u5ba1\u6279", "review", "approve",
               "\u4eba\u5de5", "\u586b\u5199", "\u63d0\u4ea4"]
    for kw in user_kw:
        if kw in lower:
            return ("bpmn:userTask", 'contains keyword "' + kw + '", inferred as user task')

    # serviceTask
    service_kw = ["\u7cfb\u7edf", "\u81ea\u52a8", "service", "auto",
                  "\u8ba1\u7b97", "\u5904\u7406"]
    for kw in service_kw:
        if kw in lower:
            return ("bpmn:serviceTask", 'contains keyword "' + kw + '", inferred as service task')

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

if __name__ == "__main__":
    # Example test
    test_mermaid = """graph TD
    start([\u5f00\u59cb])
    A[\u63d0\u4ea4\u7533\u8bf7]
    B{\u5ba1\u6279\u901a\u8fc7\uff1f}
    C[\u7cfb\u7edf\u5904\u7406]
    D[\u53d1\u9001\u901a\u77e5]
    E([\u7ed3\u675f])
    start --> A
    A --> B
    B -->|\u901a\u8fc7| C
    B -->|\u62d2\u7edd| E
    C --> D
    D --> E
    """
    nodes, edges, name = parse_mermaid(test_mermaid)
    print("Flow:", name)
    print("Nodes (" + str(len(nodes)) + "):")
    for nid, ndata in nodes.items():
        print("  " + nid + ":", ndata)
    print("Edges (" + str(len(edges)) + "):")
    for e in edges:
        print("  ", e)

    bpmn_nodes, bpmn_edges, expls = auto_map_elements(nodes, edges)
    print("\nMapped nodes (" + str(len(bpmn_nodes)) + "):")
    for nid, ndata in bpmn_nodes.items():
        print("  " + nid + ": " + ndata['bpmn_type'] + " - " + ndata['name'] + " (" + ndata['mapping_reason'] + ")")
    print("\nExplanations (" + str(len(expls)) + "):")
    for e in expls:
        print("  " + e)
