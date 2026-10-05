import re
from collections import Counter

import networkx as nx

from config import DATA_FILE_PATH, FINISH_TASK_RE, GAIN_TASK_RE
from data_loading import load_game_data


QUEST_IDS = {
    "Communism": 359,
    "Fascism": 846,
    "Ultraliberalism": 373,
    "Moralism": 367,
}

WRITE_GEXF = False          # True: writes one .gexf file per ideology (for Gephi)
OUTPUT_DIR = "."

VAR_REF_RE = re.compile(r'Variable\[["\']([^"\']+)["\']\]')
SET_VAR_CALL_RE = re.compile(
    r'SetVariableValue\(\s*["\']([^"\']+)["\']\s*,\s*(true|false)\s*\)',
    re.IGNORECASE,
)
SET_VAR_ASSIGN_RE = re.compile(
    r'Variable\[["\']([^"\']+)["\']\]\s*=\s*(true|false)', re.IGNORECASE
)
IS_TASK_ACTIVE_RE = re.compile(r'IsTaskActive\(\s*["\']([^"\']+)["\']\s*\)')
QUEST_SIGNATURE_FIELDS = {"display_condition_main", "done_condition_main", "task_reward"}
SETTER_ACTIONS = {"set_true", "set_false", "gain_task", "finish_task"}

WORD_RE = re.compile(r"\w+(?:['’]\w+)*")     # "don't" counts as 1 word; punctuation and "--" do not count


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


# ---------------------------------------------------------------------------
# Functions shared with downstream_gating.py: keep them identical in both files
# (or move them to a common module imported by both)
# ---------------------------------------------------------------------------

def find_quest_entry(data: dict, quest_id: int):
    for key, value in data.items():
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict) or "fields" not in item:
                continue
            if item.get("id") != quest_id:
                continue
            field_titles = {f.get("title") for f in item.get("fields", [])}
            if QUEST_SIGNATURE_FIELDS & field_titles:
                return key, item
    return None, None


def _var_from_expr(expr):
    if not expr:
        return None
    m = VAR_REF_RE.search(expr)
    return m.group(1) if m else None


def extract_quest_variables(quest_entry: dict) -> dict:
    fields = {f["title"]: f["value"] for f in quest_entry["fields"]}
    structure = {
        "quest_id": quest_entry["id"],
        "title": fields.get("Title"),
        "main": {
            "display": _var_from_expr(fields.get("display_condition_main")),
            "done": _var_from_expr(fields.get("done_condition_main")),
            "cancel": _var_from_expr(fields.get("cancel_condition_main")),
        },
        "subtasks": [],
    }
    for i in range(1, 30):  # wide enough to cover every case
        idx = f"{i:02d}"
        title = fields.get(f"subtask_title_{idx}")
        if not title:
            continue
        structure["subtasks"].append({
            "index": idx, "title": title,
            "display": _var_from_expr(fields.get(f"display_subtask_{idx}")),
            "done": _var_from_expr(fields.get(f"done_subtask_{idx}")),
            "cancel": _var_from_expr(fields.get(f"cancel_subtask_{idx}")),
        })
    return structure


def all_target_variables(quest_structure: dict) -> set:
    variables = set()
    variables.update(v for v in quest_structure["main"].values() if v)
    for st in quest_structure["subtasks"]:
        variables.update(v for v in (st["display"], st["done"], st["cancel"]) if v)
    return variables


def bare_task_name(var_name):
    """'TASK.get_yourself_organised' -> 'get_yourself_organised'."""
    if var_name and var_name.startswith("TASK."):
        return var_name.split(".", 1)[1]
    return var_name


def trace_chain_and_downstream(data: dict, seed_task_var: str):
    chain = {bare_task_name(seed_task_var)}
    changed = True

    while changed:
        changed = False
        for conv in data.get("conversations", []):
            for entry in conv.get("dialogueEntries", []):
                cond = entry.get("conditionsString", "") or ""
                script = entry.get("userScript", "") or ""

                gated = False
                for name in IS_TASK_ACTIVE_RE.findall(cond):
                    if bare_task_name(name) in chain:
                        gated = True
                        break
                if not gated:
                    for var_name in VAR_REF_RE.findall(cond):
                        if var_name.endswith("_done"):
                            if bare_task_name(var_name[: -len("_done")]) in chain:
                                gated = True
                                break

                calls = {
                    bare_task_name(v)
                    for v in (set(GAIN_TASK_RE.findall(script)) | set(FINISH_TASK_RE.findall(script)))
                }
                sets_chain_task = bool(calls & chain)

                if gated or sets_chain_task:
                    new_vars = calls - chain
                    if new_vars:
                        chain |= new_vars
                        changed = True

    return chain


def _get_link_dest(link: dict, current_conv_id):
    dest_conv = (
        link.get("destinationConversationID")
        or link.get("destinationConversationId")
        or link.get("DestinationConversationId")
        or current_conv_id
    )
    dest_entry = (
        link.get("destinationDialogueID")
        or link.get("destinationDialogueEntryId")
        or link.get("DestinationDialogueEntryId")
    )
    return dest_conv, dest_entry


def find_variable_events(data: dict, target_vars: set) -> list:
    events = []
    for conv in data.get("conversations", []):
        conv_id = conv.get("id")
        for entry in conv.get("dialogueEntries", []):
            script = entry.get("userScript", "") or ""
            cond = entry.get("conditionsString", "") or ""
            for var_name, val in SET_VAR_CALL_RE.findall(script) + SET_VAR_ASSIGN_RE.findall(script):
                if bare_task_name(var_name) in target_vars:
                    events.append({"conv_id": conv_id, "entry_id": entry["id"], "var": var_name,
                                    "action": "set_true" if val.lower() == "true" else "set_false"})
            for var_name in VAR_REF_RE.findall(cond):
                if bare_task_name(var_name) in target_vars:
                    events.append({"conv_id": conv_id, "entry_id": entry["id"], "var": var_name, "action": "check"})
    return events


def find_task_chain_events(data: dict, target_vars: set) -> list:
    events = []
    for conv in data.get("conversations", []):
        conv_id = conv.get("id")
        for entry in conv.get("dialogueEntries", []):
            script = entry.get("userScript", "") or ""
            for var_name in GAIN_TASK_RE.findall(script):
                if bare_task_name(var_name) in target_vars:
                    events.append({"conv_id": conv_id, "entry_id": entry["id"], "var": var_name, "action": "gain_task"})
            for var_name in FINISH_TASK_RE.findall(script):
                if bare_task_name(var_name) in target_vars:
                    events.append({"conv_id": conv_id, "entry_id": entry["id"], "var": var_name, "action": "finish_task"})
    return events


def _node_key(conv_id, entry_id) -> str:
    return f"conv{conv_id}_entry{entry_id}"


def classify_conversations(events: list) -> tuple:
    setter_convs, checker_convs = set(), set()
    for ev in events:
        (setter_convs if ev["action"] in SETTER_ACTIONS else checker_convs).add(ev["conv_id"])
    return setter_convs, checker_convs - setter_convs


# ---------------------------------------------------------------------------
# Specific to internal breadth: graph construction, words, ghost nodes
# ---------------------------------------------------------------------------

def build_conversation_graph(conv: dict) -> nx.DiGraph:
    g = nx.DiGraph()
    conv_id = conv.get("id")
    for entry in conv.get("dialogueEntries", []):
        fields = {f["title"]: f["value"] for f in entry.get("fields", [])}
        text = (fields.get("Dialogue Text") or "").strip()
        has_dialogue = bool(text) and text.lower() != "null"
        key = _node_key(conv_id, entry["id"])
        g.add_node(key, kind="dialogue_entry", has_dialogue=has_dialogue,
                    words=count_words(text) if has_dialogue else 0,   # computed on the full text
                    title=str(fields.get("Title") or "")[:80], text=text[:200],
                    actor=str(fields.get("Actor")), conv_id=str(conv_id), entry_id=str(entry["id"]))
    for entry in conv.get("dialogueEntries", []):
        src = _node_key(conv_id, entry["id"])
        for link in entry.get("outgoingLinks", []):
            dst_conv, dst_entry = _get_link_dest(link, conv_id)
            if dst_entry is None:
                continue
            g.add_edge(src, _node_key(dst_conv, dst_entry), relation="dialogue_link")
    return g


def build_internal_subgraph(data: dict, internal_conv_ids: set) -> nx.DiGraph:
    master = nx.DiGraph()
    conv_by_id = {c.get("id"): c for c in data.get("conversations", [])}
    for conv_id in sorted(internal_conv_ids):
        conv = conv_by_id.get(conv_id)
        if conv is None:
            continue
        local_graph = build_conversation_graph(conv)
        master.add_nodes_from(local_graph.nodes(data=True))
        master.add_edges_from(local_graph.edges(data=True))
    return master


def tag_gate_nodes(master: nx.DiGraph, events: list):
    for ev in events:
        key = _node_key(ev["conv_id"], ev["entry_id"])
        if key not in master:
            continue
        role = "quest_setter" if ev["action"] in SETTER_ACTIONS else "quest_gate"
        if master.nodes[key].get("node_role") != "quest_setter":
            master.nodes[key]["node_role"] = role


def apply_simple_wave_layout(master: nx.DiGraph, x_spacing: float = 220.0, y_spacing: float = 60.0):
    roots = [n for n, d in master.in_degree() if d == 0]
    if not roots and master.number_of_nodes() > 0:
        roots = [list(master.nodes())[0]]

    depths = {}
    for r in roots:
        for node, d in nx.single_source_shortest_path_length(master, r).items():
            depths[node] = min(depths.get(node, d), d)
    for node in master.nodes():
        depths.setdefault(node, 0)

    layers = {}
    for node, d in depths.items():
        layers.setdefault(d, []).append(node)

    for depth, nodes_in_layer in layers.items():
        num_nodes = len(nodes_in_layer)
        for i, node in enumerate(nodes_in_layer):
            offset = (i - (num_nodes - 1) / 2.0) * y_spacing
            master.nodes[node]["viz"] = {
                "position": {"x": float(depth * x_spacing), "y": float(-offset), "z": 0.0}
            }


def analyze_quest(data: dict, ideology: str, quest_id: int):
    _, quest_entry = find_quest_entry(data, quest_id)
    if quest_entry is None:
        print(f"[error] {ideology}: quest id={quest_id} not found.")
        return None

    structure = extract_quest_variables(quest_entry)
    own_vars = all_target_variables(structure)
    own_vars_bare = {bare_task_name(v) for v in own_vars}
    seed_task_var = structure["main"]["display"]

    chain_vars = trace_chain_and_downstream(data, seed_task_var)
    target_vars = own_vars_bare | chain_vars

    events = find_variable_events(data, target_vars) + find_task_chain_events(data, target_vars)
    internal_conv_ids, reactivity_conv_ids = classify_conversations(events)

    master = build_internal_subgraph(data, internal_conv_ids)
    tag_gate_nodes(master, events)

    # "Ghost" nodes: created by add_edges_from for links to conversations that are not included.
    # They have no attributes and must NOT be counted as nodes of the subgraph.
    real = {n for n, d in master.nodes(data=True) if "kind" in d}
    ghost = master.number_of_nodes() - len(real)
    narrative = {n for n in real if master.nodes[n]["has_dialogue"]}
    structural = real - narrative
    total_words = sum(master.nodes[n]["words"] for n in real)
    edges_real = sum(1 for u, v in master.edges() if u in real and v in real)

    # Breakdown by conversation: how much each one weighs on the subgraph
    setter_events = Counter(str(ev["conv_id"]) for ev in events if ev["action"] in SETTER_ACTIONS)
    per_conv = {}
    for n in real:
        d = master.nodes[n]
        c = per_conv.setdefault(d["conv_id"], {"nodes": 0, "narrative": 0, "words": 0})
        c["nodes"] += 1
        c["narrative"] += int(d["has_dialogue"])
        c["words"] += d["words"]

    print(f"\n=== {ideology} (Quest {quest_id}) ===")
    print(f"Declared variables: {len(own_vars)}; traced chain: {len(chain_vars)} tasks")
    print(f"Internal conversations: {sorted(internal_conv_ids)}")
    print(f"Reactivity scene     : {sorted(reactivity_conv_ids)}")
    print(f"  V_narrative  : {len(narrative)} ({100 * len(narrative) / len(real):.1f}%)")
    print(f"  V_structural : {len(structural)}")
    print(f"  Total nodes  : {len(real)}  (ghost nodes excluded: {ghost})")
    print(f"  Edges        : {edges_real} between real nodes, {master.number_of_edges()} total")
    print(f"  Words        : {total_words} ({total_words / max(len(narrative), 1):.1f} per narrative node)")
    print("  Per conversation (nodes / narrative / words / % words / setter events):")
    for conv_id, c in sorted(per_conv.items(), key=lambda kv: kv[1]["words"], reverse=True):
        share = 100 * c["words"] / total_words if total_words else 0
        print(f"    conv {conv_id}: {c['nodes']} / {c['narrative']} / {c['words']} / "
              f"{share:.1f}% / {setter_events.get(conv_id, 0)}")

    if WRITE_GEXF:
        apply_simple_wave_layout(master)
        nx.write_gexf(master, f"{OUTPUT_DIR}/{ideology.lower()}_internal.gexf")

    return {
        "ideology": ideology, "quest_id": quest_id,
        "declared_vars": len(own_vars),
        "internal_convs": len(internal_conv_ids), "reactive_convs": len(reactivity_conv_ids),
        "nodes": len(real), "ghost": ghost,
        "narrative": len(narrative), "structural": len(structural),
        "narrative_pct": 100 * len(narrative) / len(real),
        "edges_real": edges_real, "edges_total": master.number_of_edges(),
        "words": total_words,
        "top_conv_word_share": max((100 * c["words"] / total_words for c in per_conv.values()), default=0)
                               if total_words else 0,
    }


def run_pipeline(data):
    results = {}
    for ideology, quest_id in QUEST_IDS.items():
        r = analyze_quest(data, ideology, quest_id)
        if r is not None:
            results[ideology] = r

    print("\nAnalysis Summary: ")
    print("Ideology".ljust(17) + "Vars".ljust(6) + "Setter".ljust(8) + "React".ljust(7) + "Narr.".ljust(14)
          + "Struct.".ljust(9) + "Nodes".ljust(8) + "Ghost".ljust(10) + "Edges (real/tot.)".ljust(20)
          + "Words".ljust(9) + "Top conv %")
    for r in results.values():
        print(r["ideology"].ljust(17) + str(r["declared_vars"]).ljust(6) + str(r["internal_convs"]).ljust(8)
              + str(r["reactive_convs"]).ljust(7) + f"{r['narrative']} ({r['narrative_pct']:.1f}%)".ljust(14)
              + str(r["structural"]).ljust(9) + str(r["nodes"]).ljust(8) + str(r["ghost"]).ljust(10)
              + f"{r['edges_real']}/{r['edges_total']}".ljust(20) + str(r["words"]).ljust(9)
              + f"{r['top_conv_word_share']:.1f}%")
    return results


if __name__ == "__main__":
    data = load_game_data(DATA_FILE_PATH)
    run_pipeline(data)