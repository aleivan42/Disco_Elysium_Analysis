import re

import networkx as nx

from config import DATA_FILE_PATH, FINISH_TASK_RE, GAIN_TASK_RE
from data_loading import load_game_data


QUEST_ID = None 
OUTPUT_GEXF = None

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
    for i in range(1, 30): #should be long enough to find every case
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
    """ for example: 'TASK.get_yourself_organised' -> 'get_yourself_organised'."""
    if var_name and var_name.startswith("TASK."):
        return var_name.split(".", 1)[1]
    return var_name


def trace_chain_and_downstream(data: dict, seed_task_var: str):

    chain = {seed_task_var}
    changed = True

    while changed:
        changed = False
        bare_chain = {bare_task_name(v) for v in chain}

        for conv in data.get("conversations", []):
            for entry in conv.get("dialogueEntries", []):
                cond = entry.get("conditionsString", "") or ""
                script = entry.get("userScript", "") or ""

                gated = False
                for name in IS_TASK_ACTIVE_RE.findall(cond):
                    if name in bare_chain or f"TASK.{name}" in chain:
                        gated = True
                        break
                if not gated:
                    for var_name in VAR_REF_RE.findall(cond):
                        if var_name.endswith("_done"):
                            if var_name[: -len("_done")] in chain:
                                gated = True
                                break

                calls = set(GAIN_TASK_RE.findall(script)) | set(FINISH_TASK_RE.findall(script))
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
                if var_name in target_vars:
                    events.append({"conv_id": conv_id, "entry_id": entry["id"], "var": var_name,
                                    "action": "set_true" if val.lower() == "true" else "set_false"})
            for var_name in VAR_REF_RE.findall(cond):
                if var_name in target_vars:
                    events.append({"conv_id": conv_id, "entry_id": entry["id"], "var": var_name, "action": "check"})
    return events


def find_task_chain_events(data: dict, target_vars: set) -> list:
    events = []
    for conv in data.get("conversations", []):
        conv_id = conv.get("id")
        for entry in conv.get("dialogueEntries", []):
            script = entry.get("userScript", "") or ""
            for var_name in GAIN_TASK_RE.findall(script):
                if var_name in target_vars:
                    events.append({"conv_id": conv_id, "entry_id": entry["id"], "var": var_name, "action": "gain_task"})
            for var_name in FINISH_TASK_RE.findall(script):
                if var_name in target_vars:
                    events.append({"conv_id": conv_id, "entry_id": entry["id"], "var": var_name, "action": "finish_task"})
    return events


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
        g.add_node(key, kind="dialogue_entry", has_dialogue=has_dialogue,
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


def classify_conversations(events: list) -> tuple:

    setter_convs = set()
    checker_convs = set()
    for ev in events:
        if ev["action"] in SETTER_ACTIONS:
            setter_convs.add(ev["conv_id"])
        else:
            checker_convs.add(ev["conv_id"])
    reactivity_convs = checker_convs - setter_convs
    internal_convs = setter_convs
    return internal_convs, reactivity_convs


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
        current = master.nodes[key].get("node_role")
        if current != "quest_setter":
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


def run_pipeline():
    data = load_game_data(DATA_FILE_PATH)

    collection_name, quest_entry = find_quest_entry(data, QUEST_ID)
    if quest_entry is None:
        print(f"[errore] quest id={QUEST_ID} non trovata.")
        return None

    quest_structure = extract_quest_variables(quest_entry)
    own_vars = all_target_variables(quest_structure)
    seed_task_var = quest_structure["main"]["display"]

    chain_vars = trace_chain_and_downstream(data, seed_task_var)
    target_vars = own_vars | chain_vars

    print(f"Variabili proprie della Quest 359: {len(own_vars)}")
    print(f"Catena estesa tracciata dal seed ({seed_task_var!r}): {len(chain_vars)} task -- {sorted(chain_vars)}")
    print(f"Totale target_vars (unione): {len(target_vars)}")

    events = find_variable_events(data, target_vars) + find_task_chain_events(data, target_vars)

    internal_conv_ids, reactivity_conv_ids = classify_conversations(events)
    print(f"Conversazioni interne (avanzano la catena): {sorted(internal_conv_ids)}")
    print(f"Reactivity scene (leggono soltanto): {sorted(reactivity_conv_ids)}")

    master = build_internal_subgraph(data, internal_conv_ids)
    tag_gate_nodes(master, events)

    n_narrative = sum(1 for _, d in master.nodes(data=True) if d.get("has_dialogue"))
    n_structural = sum(1 for _, d in master.nodes(data=True) if not d.get("has_dialogue"))

    print(f"  V_narrative (nodes with text)   : {n_narrative}")
    print(f"  V_structural (nodes without text): {n_structural}")
    print(f"  Total internal nodes            : {master.number_of_nodes()}")
    print(f"  S_reactivity (external scenes)   : {len(reactivity_conv_ids)}")

    apply_simple_wave_layout(master)
    nx.write_gexf(master, OUTPUT_GEXF)

    return master


if __name__ == "__main__":
    run_pipeline()