from multiprocessing.spawn import prepare
import re
from collections import defaultdict, Counter

import networkx as nx

from config import (
    DATA_FILE_PATH,
    FINISH_TASK_RE,
    GAIN_TASK_RE,
    CHECK_VAR_THRESHOLD_RE,
    ISTHC_PRESENT_RE,
)
from data_loading import load_game_data


QUEST_IDS = {
    "Communism": 359,
    "Fascism": 846,
    "Ultraliberalism": 373,
    "Moralism": 367,
}

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
    for i in range(1, 13):
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


def classify_conversations(events: list) -> tuple:
    setter_convs, checker_convs = set(), set()
    for ev in events:
        (setter_convs if ev["action"] in SETTER_ACTIONS else checker_convs).add(ev["conv_id"])
    return setter_convs, checker_convs - setter_convs


def build_conversation_graph(conv: dict) -> nx.DiGraph:
    g = nx.DiGraph()
    conv_id = conv.get("id")
    for entry in conv.get("dialogueEntries", []):
        fields = {f["title"]: f["value"] for f in entry.get("fields", [])}
        text = (fields.get("Dialogue Text") or "").strip()
        has_dialogue = bool(text) and text.lower() != "null"
        key = f"conv{conv_id}_entry{entry['id']}"
        g.add_node(key, kind="dialogue_entry", has_dialogue=has_dialogue,
                    title=str(fields.get("Title") or "")[:80], text=text[:200],
                    actor=str(fields.get("Actor")), conv_id=str(conv_id), entry_id=str(entry["id"]),
                    conditions=entry.get("conditionsString", "") or "",
                    script=entry.get("userScript", "") or "")
    for entry in conv.get("dialogueEntries", []):
        src = f"conv{conv_id}_entry{entry['id']}"
        for link in entry.get("outgoingLinks", []):
            dst_conv, dst_entry = _get_link_dest(link, conv_id)
            if dst_entry is None:
                continue
            g.add_edge(src, f"conv{dst_conv}_entry{dst_entry}", relation="dialogue_link")
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


REAL_GATE_TAGS = {"external_task_dependency", "reputation_gate", "thought_gate"}
NARRATIVE_STATE_TAGS = {"scene_done_flag", "internal_counter", "other_external_condition"}

POLITICAL_THOUGHTS = {
    "Communism": "communist",
    "Fascism": "revacholian_nationhood",
    "Ultraliberalism": "ultraliberal",
    "Moralism": "moralist",
}


def classify_gate_node(cond: str, target_vars_bare: set):
    tags = set()
    involved = defaultdict(set)

    for name in IS_TASK_ACTIVE_RE.findall(cond):
        if bare_task_name(name) not in target_vars_bare:
            tags.add("external_task_dependency")
            involved["external_task_dependency"].add(name)

    thresholds = CHECK_VAR_THRESHOLD_RE.findall(cond)
    threshold_vars = {v for v, _, _ in thresholds}

    for var_name, _op, _num in thresholds:
        if bare_task_name(var_name) in target_vars_bare:
            continue
        tag = "reputation_gate" if var_name.startswith("reputation.") else "internal_counter"
        tags.add(tag)
        involved[tag].add(var_name)

    for var_name in VAR_REF_RE.findall(cond):
        if var_name in threshold_vars or bare_task_name(var_name) in target_vars_bare:
            continue
        if var_name.startswith("TASK."):
            base = bare_task_name(var_name[:-len("_done")] if var_name.endswith("_done") else var_name)
            if base in target_vars_bare:
                continue
            tag = "external_task_dependency"
        elif var_name.endswith("_done"):
            tag = "scene_done_flag"          
        else:
            tag = "other_external_condition"
        tags.add(tag)
        involved[tag].add(var_name)

    for var_name in ISTHC_PRESENT_RE.findall(cond):
        tags.add("thought_gate")
        involved["thought_gate"].add(var_name)

    return tags, involved


def compute_downstream_gating(master, target_vars_bare, foreign_tasks=frozenset(),
                              foreign_thoughts=frozenset()):
    real_nodes = {n for n, d in master.nodes(data=True) if "kind" in d}
    ghost_nodes = master.number_of_nodes() - len(real_nodes)

    nodes_by_tag = defaultdict(set)
    variables_by_tag = defaultdict(set)
    cross_nodes, any_tagged = set(), set()

    for node in real_nodes:
        cond = master.nodes[node].get("conditions", "")
        if not cond:
            continue
        tags, involved = classify_gate_node(cond, target_vars_bare)
        if tags:
            any_tagged.add(node)
        for tag in tags:
            nodes_by_tag[tag].add(node)
            variables_by_tag[tag] |= involved[tag]
        for v in involved["external_task_dependency"]:
            base = bare_task_name(v[:-len("_done")] if v.endswith("_done") else v)
            if base in foreign_tasks:
                cross_nodes.add(node)
        if involved["thought_gate"] & foreign_thoughts:
            cross_nodes.add(node)

    strict = set().union(*(nodes_by_tag[t] for t in REAL_GATE_TAGS))
    medium = strict | nodes_by_tag["scene_done_flag"]

    return {
        "total_nodes": len(real_nodes),
        "ghost_nodes": ghost_nodes,
        "strict_nodes": len(strict),        # task + reputation + Thought
        "medium_nodes": len(medium),        # as above + flag *_done non-task
        "broad_nodes": len(any_tagged),     # as above + any external reference
        "cross_ideology_nodes": len(cross_nodes),
        "tag_nodes": {t: len(n) for t, n in nodes_by_tag.items()},
        "variables_by_tag": {k: sorted(v) for k, v in variables_by_tag.items()},
    }


def prepare_quest(data: dict, quest_id: int):
    _, quest_entry = find_quest_entry(data, quest_id)
    if quest_entry is None:
        return None
    structure = extract_quest_variables(quest_entry)
    own_vars_bare = {bare_task_name(v) for v in all_target_variables(structure)}
    chain_vars = trace_chain_and_downstream(data, structure["main"]["display"])
    target_vars = own_vars_bare | chain_vars
    events = find_variable_events(data, target_vars) + find_task_chain_events(data, target_vars)
    internal_conv_ids, _ = classify_conversations(events)
    return {"target_vars": target_vars,
            "conv_ids": sorted(internal_conv_ids),
            "master": build_internal_subgraph(data, internal_conv_ids)}


def skill_check_stats(data: dict, prepared: dict):
    conv_by_id = {c["id"]: c for c in data["conversations"]}
    fields_to_check = ("DifficultyPass", "DifficultyWhite", "DifficultyRed")

    for ideology, p in prepared.items():
        dist = {name: Counter() for name in fields_to_check}
        n_entries = 0
        for cid in p["conv_ids"]:
            for e in conv_by_id[cid]["dialogueEntries"]:
                n_entries += 1
                f = {x["title"]: x["value"] for x in e.get("fields", [])}
                for name in fields_to_check:
                    if name in f:
                        dist[name][str(f[name])] += 1

        print(f"\n=== {ideology}: {n_entries} entries ===")
        for name in fields_to_check:
            total = sum(dist[name].values())
            print(f"  {name}: {total} entries with the field")
            print(f"    value distribution: {dict(sorted(dist[name].items()))}")
        active = sum(dist["DifficultyWhite"].values()) + sum(dist["DifficultyRed"].values())
        print(f"  active checks (White + Red): {active} "
              f"({100 * active / n_entries:.2f}% of entries)" if n_entries else "")


def run_pipeline(data):
    prepared = {i: prepare_quest(data, qid) for i, qid in QUEST_IDS.items()}
    prepared = {i: p for i, p in prepared.items() if p is not None}

    results = {}
    for ideology, p in prepared.items():
        foreign_tasks = set().union(
            *(q["target_vars"] for i, q in prepared.items() if i != ideology)
        ) - p["target_vars"]
        foreign_thoughts = {t for i, t in POLITICAL_THOUGHTS.items() if i != ideology}
        r = compute_downstream_gating(p["master"], p["target_vars"], foreign_tasks, foreign_thoughts)
        r["conv_ids"] = p["conv_ids"]
        results[ideology] = r

        print(f"\n=== {ideology} === convs {r['conv_ids']}")
        for tag in sorted(REAL_GATE_TAGS):          # variable lists for the real gates only
            print(f"  {tag}: {r['tag_nodes'].get(tag, 0)} nodes")
            print(f"    {r['variables_by_tag'].get(tag, [])}")

    pct = lambda n, t: f"{100 * n / t:.1f}%" if t else "-"
    print("\n=== SUMMARY ===")
    print("Ideology".ljust(17) + "Nodes".ljust(8) + "Ghost".ljust(7) + "Strict".ljust(14)
          + "Medium".ljust(14) + "Broad".ljust(14) + "Cross-ideol.")
    for ideology, r in results.items():
        t = r["total_nodes"]
        print(ideology.ljust(17) + str(t).ljust(8) + str(r["ghost_nodes"]).ljust(7)
              + f"{r['strict_nodes']} ({pct(r['strict_nodes'], t)})".ljust(14)
              + f"{r['medium_nodes']} ({pct(r['medium_nodes'], t)})".ljust(14)
              + f"{r['broad_nodes']} ({pct(r['broad_nodes'], t)})".ljust(14)
              + str(r["cross_ideology_nodes"]))

    skill_check_stats(data, prepared)               
    return results, prepared


def passive_by_skill(data, prepared, top=6):
    actor_by_id = {
        a["id"]: next((f["value"] for f in a.get("fields", []) if f["title"] == "Name"), "?")
        for a in data.get("actors", [])
    }
    conv_by_id = {c["id"]: c for c in data["conversations"]}
    for ideology, p in prepared.items():
        by_skill = Counter()
        for cid in p["conv_ids"]:
            for e in conv_by_id[cid]["dialogueEntries"]:
                f = {x["title"]: x["value"] for x in e.get("fields", [])}
                if str(f.get("DifficultyPass", "0")) not in ("", "0"):
                    try:
                        by_skill[actor_by_id.get(int(f.get("Actor")), "?")] += 1
                    except (TypeError, ValueError):
                        by_skill["?"] += 1
        print(f"\n{ideology}: {by_skill.most_common(top)}")


if __name__ == "__main__":
    data = load_game_data(DATA_FILE_PATH)
    results, prepared = run_pipeline(data)
    passive_by_skill(data, prepared)

conv_by_id = {c["id"]: c for c in data["conversations"]}
samples = {"2": [], "10": [], "0": []}
for cid in prepared["Moralism"]["conv_ids"]:
    for e in conv_by_id[cid]["dialogueEntries"]:
        f = {x["title"]: x["value"] for x in e.get("fields", [])}
        v = str(f.get("DifficultyPass", ""))
        if v in samples and len(samples[v]) < 6:
            samples[v].append((cid, e["id"], f.get("Actor"), (f.get("Dialogue Text") or "")[:70],
                               (e.get("conditionsString") or "")[:60]))
for v, rows in samples.items():
    print(f"\nDifficultyPass = {v}")
    for r in rows:
        print("  ", r)
    

def passive_by_skill(data, prepared, top=5):
    actor_by_id = {
        a["id"]: next((f["value"] for f in a.get("fields", []) if f["title"] == "Name"), "?")
        for a in data.get("actors", [])
    }
    conv_by_id = {c["id"]: c for c in data["conversations"]}
    for ideology, p in prepared.items():
        by_skill = Counter()
        for cid in p["conv_ids"]:
            for e in conv_by_id[cid]["dialogueEntries"]:
                f = {x["title"]: x["value"] for x in e.get("fields", [])}
                if str(f.get("DifficultyPass", "0")) not in ("", "0"):
                    try:
                        by_skill[actor_by_id.get(int(f.get("Actor")), "?")] += 1
                    except (TypeError, ValueError):
                        by_skill["?"] += 1
        print(f"\n{ideology}: {by_skill.most_common(top)}")