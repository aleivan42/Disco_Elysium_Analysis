import json
import re

import networkx as nx

from config import DATA_FILE_PATH, FINISH_TASK_RE, GAIN_TASK_RE
from data_loading import load_game_data

# Change ID and output as desired
QUEST_ID = 359
OUTPUT_GEXF = "get_yourself_organised_quest_tree.gexf"

VAR_REF_RE = re.compile(r'Variable\[["\']([^"\']+)["\']\]')
SET_VAR_CALL_RE = re.compile(
    r'SetVariableValue\(\s*["\']([^"\']+)["\']\s*,\s*(true|false)\s*\)',
    re.IGNORECASE,
)
SET_VAR_ASSIGN_RE = re.compile(
    r'Variable\[["\']([^"\']+)["\']\]\s*=\s*(true|false)', re.IGNORECASE
)

QUEST_SIGNATURE_FIELDS = {
    "display_condition_main",
    "done_condition_main",
    "task_reward",
}

# identify the quest object 
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


# quest structure


def _var_from_expr(expr):
    if not expr:
        return None
    m = VAR_REF_RE.search(expr)
    return m.group(1) if m else None


def bare_task_name(var_name):
    if var_name and var_name.startswith("TASK."):
        return var_name.split(".", 1)[1]
    return var_name


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

    for i in range(1, 13):
        idx = f"{i:02d}"
        title = fields.get(f"subtask_title_{idx}")
        if not title:
            continue
        structure["subtasks"].append({
            "index": idx,
            "title": title,
            "display": _var_from_expr(fields.get(f"display_subtask_{idx}")),
            "done": _var_from_expr(fields.get(f"done_subtask_{idx}")),
            "cancel": _var_from_expr(fields.get(f"cancel_subtask_{idx}")),
        })
    return structure


def all_target_variables(quest_structure: dict) -> set:
    variables = set()
    variables.update(v for v in quest_structure["main"].values() if v)
    for st in quest_structure["subtasks"]:
        variables.update(
            v for v in (st["display"], st["done"], st["cancel"]) if v
        )
    return variables

# Find all the conversations that are traversed by the questline

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


# Find all set/check events for TASK.xxx variables
def find_variable_events(data: dict, target_vars: set) -> list:
    events = []
    for conv in data.get("conversations", []):
        conv_id = conv.get("id")
        for entry in conv.get("dialogueEntries", []):
            script = entry.get("userScript", "") or ""
            cond = entry.get("conditionsString", "") or ""

            for var_name, val in SET_VAR_CALL_RE.findall(
                script
            ) + SET_VAR_ASSIGN_RE.findall(script):
                if var_name in target_vars:
                    events.append({
                        "conv_id": conv_id,
                        "entry_id": entry["id"],
                        "var": var_name,
                        "action": (
                            "set_true" if val.lower() == "true" else "set_false"
                        ),
                    })

            for var_name in VAR_REF_RE.findall(cond):
                if var_name in target_vars:
                    events.append({
                        "conv_id": conv_id,
                        "entry_id": entry["id"],
                        "var": var_name,
                        "action": "check",
                    })
    return events

# Find all GainTask/FinishTask events for TASK.xxx variables
def find_task_chain_events(data: dict, target_vars: set) -> list:
    events = []
    for conv in data.get("conversations", []):
        conv_id = conv.get("id")
        for entry in conv.get("dialogueEntries", []):
            script = entry.get("userScript", "") or ""

            for var_name in GAIN_TASK_RE.findall(script):
                if var_name in target_vars:
                    events.append({
                        "conv_id": conv_id,
                        "entry_id": entry["id"],
                        "var": var_name,
                        "action": "gain_task",
                    })

            for var_name in FINISH_TASK_RE.findall(script):
                if var_name in target_vars:
                    events.append({
                        "conv_id": conv_id,
                        "entry_id": entry["id"],
                        "var": var_name,
                        "action": "finish_task",
                    })
    return events


# Graph Construction (WIP)


def _node_key(conv_id, entry_id) -> str:
    return f"conv{conv_id}_entry{entry_id}"


def build_conversation_graph(conv: dict) -> nx.DiGraph:
    g = nx.DiGraph()
    conv_id = conv.get("id")

    for entry in conv.get("dialogueEntries", []):
        fields = {f["title"]: f["value"] for f in entry.get("fields", [])}
        text = (fields.get("Dialogue Text") or "").strip()
        has_dialogue = bool(text) and text.lower() != "null"
        key = _node_key(conv_id, entry["id"])
        g.add_node(
            key,
            kind="dialogue_entry",
            node_role="dialogue",
            has_dialogue=has_dialogue,
            is_root=bool(entry.get("isRoot")),
            title=str(fields.get("Title") or "")[:80],
            text=text[:200],
            actor=str(fields.get("Actor")),
            conv_id=str(conv_id),
            entry_id=str(entry["id"]),
            quest_var="",
            quest_action="",
        )

    for entry in conv.get("dialogueEntries", []):
        src = _node_key(conv_id, entry["id"])
        for link in entry.get("outgoingLinks", []):
            dst_conv, dst_entry = _get_link_dest(link, conv_id)
            if dst_entry is None:
                continue
            dst = _node_key(dst_conv, dst_entry)
            g.add_edge(src, dst, relation="dialogue_link")

    return g


ROLE_PRIORITY = {"dialogue": 0, "quest_gate": 1, "quest_setter": 2}
SETTER_ACTIONS = {"set_true", "set_false", "gain_task", "finish_task"}


def build_master_graph(
    data: dict, quest_structure: dict, all_events: list
) -> nx.DiGraph:
    master = nx.DiGraph()

    def add_state_node(var, kind, label):
        if not var:
            return
        master.add_node(
            var,
            kind=kind,
            node_role="quest_state",
            has_dialogue=False,
            is_root=False,
            title=label,
            text="",
            actor="",
            conv_id="",
            entry_id="",
            quest_var=var,
            quest_action="",
        )

    add_state_node(
        quest_structure["main"]["display"],
        "quest_start",
        quest_structure["title"],
    )
    add_state_node(quest_structure["main"]["done"], "quest_success", "SUCCESS")
    add_state_node(quest_structure["main"]["cancel"], "quest_fail", "FAIL")
    for st in quest_structure["subtasks"]:
        add_state_node(st["display"], "subtask_start", st["title"])
        add_state_node(st["done"], "subtask_done", st["title"] + " [done]")
        add_state_node(st["cancel"], "subtask_cancel", st["title"] + " [cancelled]")

    relevant_conv_ids = sorted({ev["conv_id"] for ev in all_events})
    conv_by_id = {c.get("id"): c for c in data.get("conversations", [])}
    for conv_id in relevant_conv_ids:
        conv = conv_by_id.get(conv_id)
        if conv is None:
            continue
        local_graph = build_conversation_graph(conv)
        master.add_nodes_from(local_graph.nodes(data=True))
        master.add_edges_from(local_graph.edges(data=True))

    for ev in all_events:
        node_key = _node_key(ev["conv_id"], ev["entry_id"])
        if node_key not in master:
            continue

        is_setter = ev["action"] in SETTER_ACTIONS
        new_role = "quest_setter" if is_setter else "quest_gate"
        current_role = master.nodes[node_key].get("node_role", "dialogue")
        if ROLE_PRIORITY.get(new_role, 0) >= ROLE_PRIORITY.get(current_role, 0):
            master.nodes[node_key]["node_role"] = new_role

        prev_vars = [
            v
            for v in master.nodes[node_key].get("quest_var", "").split(";")
            if v
        ]
        master.nodes[node_key]["quest_var"] = ";".join(
            sorted(set(prev_vars + [ev["var"]]))
        )
        prev_actions = [
            a
            for a in master.nodes[node_key].get("quest_action", "").split(";")
            if a
        ]
        master.nodes[node_key]["quest_action"] = ";".join(
            sorted(set(prev_actions + [ev["action"]]))
        )

        if is_setter:
            master.add_edge(node_key, ev["var"], relation=ev["action"])
        else:
            master.add_edge(ev["var"], node_key, relation="checked_by")

    return master

def apply_tree_layout(
    G: nx.DiGraph, direction: str = "LR", x_spacing: float = 250.0, y_spacing: float = 80.0
):
    roots = [n for n, d in G.in_degree() if d == 0]
    if not roots and G.number_of_nodes() > 0:
        roots = [list(G.nodes())[0]]

    depths = {}
    for root in roots:
        try:
            lengths = nx.single_source_shortest_path_length(G, root)
            for node, dist in lengths.items():
                depths[node] = max(depths.get(node, 0), dist)
        except Exception:
            pass

    for node in G.nodes():
        if node not in depths:
            depths[node] = 0

    layers = {}
    for node, depth in depths.items():
        layers.setdefault(depth, []).append(node)

    for depth, nodes_in_layer in layers.items():
        num_nodes = len(nodes_in_layer)
        for i, node in enumerate(nodes_in_layer):
            offset = (i - (num_nodes - 1) / 2.0) * y_spacing
            main_axis = depth * x_spacing

            if direction == "LR":
                x, y = main_axis, -offset
            else:  # Top to Bottom
                x, y = offset, -main_axis

            G.nodes[node]["viz"] = {
                "position": {"x": float(x), "y": float(y), "z": 0.0}
            }


# print diagnostics


def print_summary(quest_structure: dict, all_events: list):
    by_var = {}
    for ev in all_events:
        by_var.setdefault(ev["var"], []).append(ev)

    all_vars = all_target_variables(quest_structure)
    print("=" * 70)
    print(
        f"QUEST: {quest_structure['title']}  (id {quest_structure['quest_id']})"
    )
    print("=" * 70)
    for var in sorted(all_vars):
        hits = by_var.get(var, [])
        actions = (
            ", ".join(sorted({h["action"] for h in hits}))
            if hits
            else "NO EVENT"
        )
        print(f"  {var}: {len(hits)} evento/i ({actions})")


def find_quest_entry_point(master: nx.DiGraph, quest_structure: dict):
   
    start_var = quest_structure["main"]["display"]

    print("\n" + "=" * 70)
    print("QUEST ENTRY POINT:")
    print("=" * 70)

    if not start_var or start_var not in master:
        print(f"  Entry variable not found in graph: {start_var!r}")
        return None

    setters = [
        n for n in master.predecessors(start_var)
        if master.nodes[n].get("kind") == "dialogue_entry"
    ]
    dialogue_roots = [
        n for n, d in master.nodes(data=True)
        if d.get("kind") == "dialogue_entry" and d.get("is_root")
    ]

    print(f"  Variable display_condition_main: {start_var}")
    print(f"  Dialogue nodes setting the variable ({len(setters)}):")
    for n in setters:
        d = master.nodes[n]
        print(f"    - {n}: conv={d.get('conv_id')} entry={d.get('entry_id')} "
              f"actor={d.get('actor')} text={d.get('text')!r}")

    print(f"  Root nodes in sub-corpus: ({len(dialogue_roots)}):")
    for n in dialogue_roots:
        d = master.nodes[n]
        print(f"    - {n}: conv={d.get('conv_id')} entry={d.get('entry_id')} "
              f"actor={d.get('actor')} text={d.get('text')!r}")

    return {"start_var": start_var, "setters": setters, "dialogue_roots": dialogue_roots}


def find_true_conversation_root(data: dict, conv_id: int):
   
    conv_by_id = {c.get("id"): c for c in data.get("conversations", [])}
    conv = conv_by_id.get(conv_id)
    if conv is None:
        print(f"  [!] conversation {conv_id} not found in data.")
        return None

    all_entries = conv.get("dialogueEntries", [])
    root_entries = [e for e in all_entries if e.get("isRoot")]

    print(f"\n  Conversation {conv_id}: {len(all_entries)} total entries "
          f"(raw data), {len(root_entries)} with isRoot=True.")

    if not root_entries:
        print("    No node with isRoot=True was found.")
        return {"conv_id": conv_id, "conv": conv, "root_entry": None,
                "all_entries_count": len(all_entries)}

    if len(root_entries) > 1:
        print(f"    Warning: {len(root_entries)} entry marked as isRoot=True "
              f"(expected 1): {[e.get('id') for e in root_entries]}")

    root_entry = root_entries[0]
    fields = {f["title"]: f["value"] for f in root_entry.get("fields", [])}
    print(f"    Real root found: entry_id={root_entry.get('id')} "
          f"actor={fields.get('Actor')} "
          f"text={(fields.get('Dialogue Text') or '')[:200]!r}")

    return {"conv_id": conv_id, "conv": conv, "root_entry": root_entry,
            "all_entries_count": len(all_entries)}


def debug_entry_point_rootedness(data: dict, entry_point_result: dict):
    if not entry_point_result:
        print("\n[debug root] no entry_point_result.")
        return

    setters = entry_point_result.get("setters", [])
    if not setters:
        print("\n[debug root] no setter to analyze.")
        return

    print("\n" + "=" * 70)
    print("DEBUG: Real root node:")
    print("=" * 70)

    for setter_key in setters:
        m = re.match(r"conv(\d+)_entry(\d+)", setter_key)
        if not m:
            print(f"  [!] key is impossible to parse: {setter_key}")
            continue
        conv_id, setter_entry_id = int(m.group(1)), int(m.group(2))

        root_info = find_true_conversation_root(data, conv_id)
        if root_info is None or root_info["root_entry"] is None:
            continue

        conv = root_info["conv"]
        root_entry_id = root_info["root_entry"].get("id")

        local_graph = build_conversation_graph(conv)
        root_key = _node_key(conv_id, root_entry_id)
        setter_key_full = _node_key(conv_id, setter_entry_id)

        if root_key not in local_graph or setter_key_full not in local_graph:
            print(
                  f"(root_key={root_key in local_graph}, "
                  f"setter_key={setter_key_full in local_graph}).")
            continue

        reachable = nx.has_path(local_graph, root_key, setter_key_full)
        print(f"    Setter (entry {setter_entry_id}) reachable from root "
              f"(entry {root_entry_id}) following dialogue links: {reachable}")

        if reachable:
            path = nx.shortest_path(local_graph, root_key, setter_key_full)
            print(f"    Path ({len(path)} nodi): {' -> '.join(path)}")


TASK_FUNC_RE = re.compile(r'(\w+Task)\(')


def diagnose_zero_hit_variables(
    data: dict, quest_structure: dict, all_events: list
):
    found_vars = {ev["var"] for ev in all_events}
    zero_hit = sorted(
        v for v in all_target_variables(quest_structure) if v not in found_vars
    )
    if not zero_hit:
        return

    print("\n" + "=" * 70)
    print("Looking for zero-hits variables")
    print("=" * 70)

    task_funcs = set()
    for conv in data.get("conversations", []):
        for entry in conv.get("dialogueEntries", []):
            task_funcs.update(
                TASK_FUNC_RE.findall(entry.get("userScript", "") or "")
            )
    print(
        f"Funzioni '...Task(' found in corpus: {sorted(task_funcs)}"
    )

    for var in zero_hit:
        print(f"\n--- Raw search for: {var} ---")
        hits = 0
        for conv in data.get("conversations", []):
            conv_id = conv.get("id")
            for entry in conv.get("dialogueEntries", []):
                script = entry.get("userScript", "") or ""
                cond = entry.get("conditionsString", "") or ""
                on_execute_blob = json.dumps(entry.get("onExecute", {}))
                if var in script or var in cond or var in on_execute_blob:
                    hits += 1
                    where = []
                    if var in script:
                        where.append(f"userScript={script!r}")
                    if var in cond:
                        where.append(f"conditionsString={cond!r}")
                    if var in on_execute_blob:
                        where.append("onExecute=<presente, vedi sotto>")
                        print(
                            f"    conv {conv_id} / entry {entry['id']}:"
                            f" {'; '.join(where)}"
                        )
                        print(f"        onExecute raw: {on_execute_blob[:500]}")
                        continue
                    print(
                        f"    conv {conv_id} / entry {entry['id']}:"
                        f" {'; '.join(where)}"
                    )
        if hits == 0:
            print("  No occurrence for the variable.")


# Main pipeline


def run_pipeline():
    data = load_game_data(DATA_FILE_PATH)

    collection_name, quest_entry = find_quest_entry(data, QUEST_ID)
    if quest_entry is None:
        print(
            f"[error] quest id={QUEST_ID} not found. Available keys:"
            f" {list(data.keys())}"
        )
        return None

    quest_structure = extract_quest_variables(quest_entry)
    target_vars = all_target_variables(quest_structure)

    events = find_variable_events(data, target_vars) + find_task_chain_events(
        data, target_vars
    )

    print_summary(quest_structure, events)
    diagnose_zero_hit_variables(data, quest_structure, events)

    master = build_master_graph(data, quest_structure, events)

    entry_point_result = find_quest_entry_point(master, quest_structure)

    debug_entry_point_rootedness(data, entry_point_result)

    # Applicazione del layout ad albero temporale (Da Sinistra a Destra "LR")
    apply_tree_layout(master, direction="LR")

    nx.write_gexf(master, OUTPUT_GEXF)

    n_dialogue = sum(
        1
        for _, d in master.nodes(data=True)
        if d.get("kind") == "dialogue_entry"
    )
    n_gate = sum(
        1 for _, d in master.nodes(data=True) if d.get("node_role") == "quest_gate"
    )
    n_setter = sum(
        1
        for _, d in master.nodes(data=True)
        if d.get("node_role") == "quest_setter"
    )
    print(
        f"\nGraph exported in '{OUTPUT_GEXF}': {master.number_of_nodes()} nodes,"
        f" {master.number_of_edges()} arches ({n_dialogue} dialogue nodes, of"
        f" which {n_gate} 'quest_gate' and {n_setter} 'quest_setter')."
    )
    print(
        "Tree layout (Left-to-Right) applied successfully to nodes coordinates."
    )
    return master


if __name__ == "__main__":
    run_pipeline()