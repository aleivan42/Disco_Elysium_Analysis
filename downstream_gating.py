import re
from collections import defaultdict

import networkx as nx

from config import (
    DATA_FILE_PATH,
    FINISH_TASK_RE,
    GAIN_TASK_RE,
    CHECK_VAR_THRESHOLD_RE,
    REPUTATION_GROWS_RE,
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


# ---------------------------------------------------------------------------
# 0. Quest object + chain tracing (invariato rispetto allo script di
#    Internal Breadth -- stessa fonte di verità per target_vars e
#    internal_conv_ids, cosi' il downstream gating e' misurato esattamente
#    sullo stesso sottografo gia' riportato in Tabella 4.2)
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
    """'TASK.get_yourself_organised' -> 'get_yourself_organised'."""
    if var_name and var_name.startswith("TASK."):
        return var_name.split(".", 1)[1]
    return var_name


def trace_chain_and_downstream(data: dict, seed_task_var: str):
    """Identica alla versione usata per l'Internal Breadth (Tabella 4.2)."""
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


# ---------------------------------------------------------------------------
# 1. Downstream gating: classificazione dei punti di attrito
#
#    Un nodo del sottografo interno e' un "gate" se la sua conditionsString
#    referenzia stato che NON fa parte delle variabili proprie della quest
#    (target_vars): quelle sono semplice bookkeeping interno (gia' contate
#    in Internal Breadth), non un ostacolo imposto al giocatore. Le
#    categorie NON sono mutuamente esclusive: un nodo con una condizione
#    composta (es. task dependency AND thought gate) viene taggato in
#    entrambe.
#
#    Distinzione REAL_GATE_TAGS vs NARRATIVE_STATE_TAGS: ispezionando le
#    variabili effettivamente catturate, "stat_or_skill_threshold" e
#    "other_external_condition" risultano quasi interamente contatori
#    narrativi e flag di variante cosmetica (meteo, saluti gia' dati,
#    quante volte hai chiesto una cosa), non vera frizione strutturale.
#    Solo external_task_dependency, reputation_gate e thought_gate
#    rappresentano barriere che condizionano l'accesso a contenuto, non
#    solo la sua variazione superficiale.
# ---------------------------------------------------------------------------

REAL_GATE_TAGS = {"external_task_dependency", "reputation_gate", "thought_gate"}
NARRATIVE_STATE_TAGS = {"stat_or_skill_threshold", "other_external_condition"}


def classify_gate_node(cond: str, target_vars_bare: set):
    """Ritorna un set di categorie di gate rilevate in questa conditionsString,

    piu' i nomi di variabile/task coinvolti per categoria (per audit
    manuale). Nulla viene ritornato per condizioni vuote o che
    referenziano solo variabili proprie della quest.
    """
    tags = set()
    involved = defaultdict(set)

    for name in IS_TASK_ACTIVE_RE.findall(cond):
        if bare_task_name(name) not in target_vars_bare:
            tags.add("external_task_dependency")
            involved["external_task_dependency"].add(name)

    for var_name in VAR_REF_RE.findall(cond):
        if var_name.endswith("_done"):
            base = bare_task_name(var_name[: -len("_done")])
            if base not in target_vars_bare:
                tags.add("external_task_dependency")
                involved["external_task_dependency"].add(var_name)

    for var_name, _op, _num in CHECK_VAR_THRESHOLD_RE.findall(cond):
        if bare_task_name(var_name) in target_vars_bare:
            continue
        if var_name.startswith("reputation."):
            tags.add("reputation_gate")
            involved["reputation_gate"].add(var_name)
        else:
            tags.add("stat_or_skill_threshold")
            involved["stat_or_skill_threshold"].add(var_name)

    for var_name in ISTHC_PRESENT_RE.findall(cond):
        tags.add("thought_gate")
        involved["thought_gate"].add(var_name)

    # Variabili booleane esterne referenziate ma non gia' coperte sopra
    # (es. flag di mondo, esiti di altre quest, stato di un NPC).
    threshold_vars = {v for v, _, _ in CHECK_VAR_THRESHOLD_RE.findall(cond)}
    for var_name in VAR_REF_RE.findall(cond):
        if var_name.endswith("_done") or var_name in threshold_vars:
            continue
        if bare_task_name(var_name) in target_vars_bare:
            continue
        tags.add("other_external_condition")
        involved["other_external_condition"].add(var_name)

    return tags, involved


def compute_downstream_gating(master: nx.DiGraph, target_vars_bare: set):
    tag_counts = defaultdict(int)
    variables_by_tag = defaultdict(set)
    nodes_by_tag = defaultdict(set)
    any_tagged_nodes = set()

    for node, attrs in master.nodes(data=True):
        cond = attrs.get("conditions", "")
        if not cond:
            continue
        tags, involved = classify_gate_node(cond, target_vars_bare)
        if tags:
            any_tagged_nodes.add(node)
        for tag in tags:
            tag_counts[tag] += 1
            variables_by_tag[tag] |= involved[tag]
            nodes_by_tag[tag].add(node)

    # Nodi deduplicati che portano ALMENO una vera barriera strutturale
    # (task dependency incrociata, reputation gate, thought gate), a
    # differenza di any_tagged_nodes che include anche il rumore
    # narrativo/cosmetico di NARRATIVE_STATE_TAGS.
    real_gate_nodes = set()
    for tag in REAL_GATE_TAGS:
        real_gate_nodes |= nodes_by_tag.get(tag, set())

    narrative_state_nodes = set()
    for tag in NARRATIVE_STATE_TAGS:
        narrative_state_nodes |= nodes_by_tag.get(tag, set())

    return {
        "total_gate_nodes": len(any_tagged_nodes),
        "real_gate_nodes": len(real_gate_nodes),
        "narrative_state_nodes": len(narrative_state_nodes),
        "tag_counts": dict(tag_counts),
        "variables_by_tag": {k: sorted(v) for k, v in variables_by_tag.items()},
    }


# ---------------------------------------------------------------------------
# 2. Pipeline principale: le 4 quest in un unico giro
# ---------------------------------------------------------------------------


def analyze_quest(data: dict, ideology: str, quest_id: int):
    _, quest_entry = find_quest_entry(data, quest_id)
    if quest_entry is None:
        print(f"[errore] {ideology}: quest id={quest_id} non trovata.")
        return None

    quest_structure = extract_quest_variables(quest_entry)
    own_vars = all_target_variables(quest_structure)
    seed_task_var = quest_structure["main"]["display"]

    chain_vars = trace_chain_and_downstream(data, seed_task_var)
    own_vars_bare = {bare_task_name(v) for v in own_vars}
    target_vars = own_vars_bare | chain_vars

    events = find_variable_events(data, target_vars) + find_task_chain_events(data, target_vars)
    internal_conv_ids, _reactivity_conv_ids = classify_conversations(events)

    master = build_internal_subgraph(data, internal_conv_ids)
    gating = compute_downstream_gating(master, target_vars)

    total_nodes = master.number_of_nodes()
    real_pct = 100 * gating["real_gate_nodes"] / total_nodes if total_nodes else 0.0

    print(f"\n=== {ideology} (Quest {quest_id}) ===")
    print(f"Conversazioni attraversate (internal_conv_ids): {sorted(internal_conv_ids)}")
    print(f"Nodi totali nel sottografo                        : {total_nodes}")
    print(f"Nodi con QUALSIASI riferimento esterno (con rumore): {gating['total_gate_nodes']}")
    print(f"Nodi con narrative-state noise soltanto            : {gating['narrative_state_nodes']}")
    print(f"Nodi con almeno una barriera STRUTTURALE REALE     : {gating['real_gate_nodes']} "
          f"({real_pct:.1f}% dei nodi totali)")
    for tag, count in sorted(gating["tag_counts"].items()):
        kind = "REAL GATE" if tag in REAL_GATE_TAGS else "narrative state (escluso dal conteggio reale)"
        print(f"  - {tag:<26}: {count} occorrenze [{kind}]")
        print(f"      variabili coinvolte: {gating['variables_by_tag'][tag]}")

    return {
        "ideology": ideology,
        "quest_id": quest_id,
        "internal_conv_ids": sorted(internal_conv_ids),
        "total_nodes": total_nodes,
        "real_gate_pct": real_pct,
        **gating,
    }


def run_pipeline():
    data = load_game_data(DATA_FILE_PATH)
    results = {}
    for ideology, quest_id in QUEST_IDS.items():
        results[ideology] = analyze_quest(data, ideology, quest_id)

    print("\n=== RIEPILOGO (per Tabella Downstream Gating) ===")
    print(
        "Ideology".ljust(18)
        + "Total Nodes".ljust(14)
        + "Real Gate Nodes".ljust(18)
        + "Real Gate %".ljust(14)
    )
    for ideology, r in results.items():
        if r is None:
            continue
        print(
            ideology.ljust(18)
            + str(r["total_nodes"]).ljust(14)
            + str(r["real_gate_nodes"]).ljust(18)
            + f"{r['real_gate_pct']:.1f}%".ljust(14)
        )

    print("\n(per categoria, occorrenze grezze non deduplicate -- solo per riferimento)")
    all_tags = sorted({tag for r in results.values() if r for tag in r["tag_counts"]})
    header = "Ideology".ljust(18) + "".join(t[:20].ljust(22) for t in all_tags)
    print(header)
    for ideology, r in results.items():
        if r is None:
            continue
        row = ideology.ljust(18)
        row += "".join(str(r["tag_counts"].get(t, 0)).ljust(22) for t in all_tags)
        print(row)

    return results


if __name__ == "__main__":
    run_pipeline()