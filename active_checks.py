# active_checks.py  --  standalone script
#
# Finds the active checks (White and Red) in the subgraphs of the four Political Vision Quests
# and, for each of them, tries to determine whether failing it loses access to tasks of the quest.
#
# Usage:
#   python active_checks.py                       # uses DATA_FILE_PATH from config.py
#   python active_checks.py path/to/file.json
#   python active_checks.py file.json --csv checks.csv
#
# If config.py / data_loading.py are present it uses them (already validated patterns and loading),
# otherwise it uses local patterns and json.load. It only depends on networkx.

import argparse
import csv
import json
import re
from collections import Counter, defaultdict

import networkx as nx

try:
    from config import DATA_FILE_PATH, FINISH_TASK_RE, GAIN_TASK_RE
    _FALLBACK_PATTERNS = False
except ImportError:
    DATA_FILE_PATH = None
    GAIN_TASK_RE = re.compile(r'GainTask\(\s*["\']([^"\']+)["\']')
    FINISH_TASK_RE = re.compile(r'FinishTask\(\s*["\']([^"\']+)["\']')
    _FALLBACK_PATTERNS = True

QUEST_IDS = {
    "Communism": 359,
    "Fascism": 846,
    "Ultraliberalism": 373,
    "Moralism": 367,
}

# Internal conversations reported in Table 4.2: used to verify that the
# reconstruction of the subgraphs is identical to the one used in Chapter 4.
EXPECTED_CONVS = {
    "Communism": [358, 362, 365, 379, 944],
    "Fascism": [10, 207, 635, 847, 850],
    "Ultraliberalism": [372, 377, 546, 944, 997, 1030],
    "Moralism": [29, 366, 368, 371, 380, 549, 554, 558, 892],
}

VAR_REF_RE = re.compile(r'Variable\[["\']([^"\']+)["\']\]')
SET_VAR_CALL_RE = re.compile(
    r'SetVariableValue\(\s*["\']([^"\']+)["\']\s*,\s*(true|false)\s*\)', re.IGNORECASE)
SET_VAR_ASSIGN_RE = re.compile(
    r'Variable\[["\']([^"\']+)["\']\]\s*=\s*(true|false)', re.IGNORECASE)
IS_TASK_ACTIVE_RE = re.compile(r'IsTaskActive\(\s*["\']([^"\']+)["\']\s*\)')
QUEST_SIGNATURE_FIELDS = {"display_condition_main", "done_condition_main", "task_reward"}
SETTER_ACTIONS = {"set_true", "set_false", "gain_task", "finish_task"}


# ---------------------------------------------------------------------------
# 1. Data loading
# ---------------------------------------------------------------------------

def load_data(path: str) -> dict:
    try:
        from data_loading import load_game_data
        return load_game_data(path)
    except ImportError:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)


# ---------------------------------------------------------------------------
# 2. Subgraph reconstruction (same logic as Internal Breadth / Downstream Gating)
# ---------------------------------------------------------------------------

def bare_task_name(var_name):
    if var_name and var_name.startswith("TASK."):
        return var_name.split(".", 1)[1]
    return var_name


def _key(conv_id, entry_id) -> str:
    return f"conv{conv_id}_entry{entry_id}"


def find_quest_entry(data: dict, quest_id: int):
    for key, value in data.items():
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict) or "fields" not in item:
                continue
            if item.get("id") != quest_id:
                continue
            titles = {f.get("title") for f in item.get("fields", [])}
            if QUEST_SIGNATURE_FIELDS & titles:
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
        "main": {
            "display": _var_from_expr(fields.get("display_condition_main")),
            "done": _var_from_expr(fields.get("done_condition_main")),
            "cancel": _var_from_expr(fields.get("cancel_condition_main")),
        },
        "subtasks": [],
    }
    for i in range(1, 30):
        idx = f"{i:02d}"
        if not fields.get(f"subtask_title_{idx}"):
            continue
        structure["subtasks"].append({
            "display": _var_from_expr(fields.get(f"display_subtask_{idx}")),
            "done": _var_from_expr(fields.get(f"done_subtask_{idx}")),
            "cancel": _var_from_expr(fields.get(f"cancel_subtask_{idx}")),
        })
    return structure


def all_target_variables(structure: dict) -> set:
    variables = {v for v in structure["main"].values() if v}
    for st in structure["subtasks"]:
        variables.update(v for v in st.values() if v)
    return variables


def trace_chain_and_downstream(data: dict, seed_task_var: str) -> set:
    chain = {bare_task_name(seed_task_var)}
    changed = True
    while changed:
        changed = False
        for conv in data.get("conversations", []):
            for entry in conv.get("dialogueEntries", []):
                cond = entry.get("conditionsString", "") or ""
                script = entry.get("userScript", "") or ""

                gated = any(bare_task_name(n) in chain for n in IS_TASK_ACTIVE_RE.findall(cond))
                if not gated:
                    for var_name in VAR_REF_RE.findall(cond):
                        if var_name.endswith("_done") and \
                                bare_task_name(var_name[: -len("_done")]) in chain:
                            gated = True
                            break

                calls = {bare_task_name(v) for v in
                         set(GAIN_TASK_RE.findall(script)) | set(FINISH_TASK_RE.findall(script))}
                if gated or (calls & chain):
                    new_vars = calls - chain
                    if new_vars:
                        chain |= new_vars
                        changed = True
    return chain


def find_setter_conversations(data: dict, target_vars: set) -> set:
    """Conversations with at least one event that advances the quest (gain/finish/set variable)."""
    convs = set()
    for conv in data.get("conversations", []):
        for entry in conv.get("dialogueEntries", []):
            script = entry.get("userScript", "") or ""
            names = {bare_task_name(v) for v in
                     GAIN_TASK_RE.findall(script) + FINISH_TASK_RE.findall(script)}
            names |= {bare_task_name(v) for v, _ in
                      SET_VAR_CALL_RE.findall(script) + SET_VAR_ASSIGN_RE.findall(script)}
            if names & target_vars:
                convs.add(conv.get("id"))
                break
    return convs


def _link_dest(link: dict, current_conv_id):
    dest_conv = (link.get("destinationConversationID") or link.get("destinationConversationId")
                 or link.get("DestinationConversationId") or current_conv_id)
    dest_entry = (link.get("destinationDialogueID") or link.get("destinationDialogueEntryId")
                  or link.get("DestinationDialogueEntryId"))
    return dest_conv, dest_entry


def build_internal_subgraph(data: dict, conv_ids: set) -> nx.DiGraph:
    master = nx.DiGraph()
    conv_by_id = {c.get("id"): c for c in data.get("conversations", [])}
    for conv_id in sorted(conv_ids):
        conv = conv_by_id.get(conv_id)
        if conv is None:
            continue
        for entry in conv.get("dialogueEntries", []):
            fields = {f["title"]: f["value"] for f in entry.get("fields", [])}
            text = (fields.get("Dialogue Text") or "").strip()
            master.add_node(_key(conv_id, entry["id"]), kind="dialogue_entry",
                            has_dialogue=bool(text) and text.lower() != "null",
                            text=text[:200], script=entry.get("userScript", "") or "")
        for entry in conv.get("dialogueEntries", []):
            for link in entry.get("outgoingLinks", []):
                dst_conv, dst_entry = _link_dest(link, conv_id)
                if dst_entry is not None:
                    master.add_edge(_key(conv_id, entry["id"]), _key(dst_conv, dst_entry))
    return master


def build_prepared(data: dict) -> dict:
    prepared = {}
    for ideology, quest_id in QUEST_IDS.items():
        _, quest_entry = find_quest_entry(data, quest_id)
        if quest_entry is None:
            print(f"[error] {ideology}: quest id={quest_id} not found.")
            continue
        structure = extract_quest_variables(quest_entry)
        own_vars = {bare_task_name(v) for v in all_target_variables(structure)}
        chain = trace_chain_and_downstream(data, structure["main"]["display"])
        target_vars = own_vars | chain
        conv_ids = find_setter_conversations(data, target_vars)
        prepared[ideology] = {"target_vars": target_vars,
                              "conv_ids": sorted(conv_ids),
                              "master": build_internal_subgraph(data, conv_ids)}
    return prepared


# ---------------------------------------------------------------------------
# 3. Active check search
# ---------------------------------------------------------------------------

def _setter_names(script: str, target_vars: set) -> set:
    """Quest tasks/variables that this script advances (empty if it is not a setter)."""
    names = {bare_task_name(v) for v in GAIN_TASK_RE.findall(script) + FINISH_TASK_RE.findall(script)}
    names |= {bare_task_name(v) for v, _ in SET_VAR_CALL_RE.findall(script) + SET_VAR_ASSIGN_RE.findall(script)}
    return names & target_vars


def _branch_kind(cond: str, flag: str):
    """'failure' if the condition negates the flag, 'success' if it reads it positively, otherwise None."""
    f = re.escape(flag)
    var = rf'Variable\[["\']{f}["\']\]'
    negated = (rf'not\s*\(?\s*{var}|{var}\s*\)?\s*(?:==\s*(?:false|0)|~=\s*true|!=\s*true)')
    if re.search(negated, cond, re.I):
        return "failure"
    if re.search(var, cond):
        return "success"
    return None


def _is_plain_positive(cond: str, flag: str) -> bool:
    """True if the condition is exactly Variable["flag"] (or == true), with nothing else."""
    f = re.escape(flag)
    return bool(re.fullmatch(rf'\s*\(?\s*Variable\[["\']{f}["\']\]\s*(?:==\s*true)?\s*\)?\s*', cond, re.I))


def find_branches(entries: dict, conv_id, check_id, flag: str, max_depth: int = 4):
    """Success and failure branches of a check.

    Breadth-first visit (max_depth levels) of the entries that follow the check, stopping at the nodes whose
    conditionsString reads the flag. Success is the plain form Variable["flag"]; failure is a recognised
    negation, or (by EXCLUSION) another entry that reads the flag in a different way,
    or a sibling with no condition on the flag. In the cases by exclusion the result is INFERRED.
    Returns (success, failure, inferred, branch conditions).
    """
    flagged = []                      # (entry, kind) for the entries that read the flag
    children_of = {}
    seen, frontier = {check_id}, [check_id]
    for _ in range(max_depth):
        nxt = []
        for eid in frontier:
            kids = []
            for link in entries[eid].get("outgoingLinks", []):
                dst_conv, dst = _link_dest(link, conv_id)
                if dst_conv == conv_id and dst in entries:
                    kids.append(dst)
            children_of[eid] = kids
            for child in kids:
                if child in seen:
                    continue
                seen.add(child)
                kind = _branch_kind(entries[child].get("conditionsString", "") or "", flag) if flag else None
                if kind:
                    flagged.append((child, kind))
                else:
                    nxt.append(child)
        frontier = nxt
        if not frontier:
            break

    cond_of = lambda i: entries[i].get("conditionsString", "") or ""
    success = [c for c, k in flagged if k == "success" and _is_plain_positive(cond_of(c), flag)]
    failure = [c for c, k in flagged if k == "failure"]
    other = [c for c, k in flagged if k == "success" and c not in success]
    inferred = False
    if success and not failure and other:
        # one branch is the plain form of the flag, the other reads it differently: by exclusion it is the failure
        failure, inferred = other, True
    elif not success:
        success = other               # no "plain" branch: the reading as success is kept

    if success and not failure:
        marked = set(success)
        siblings = []
        for parent, kids in children_of.items():
            if marked & set(kids):
                siblings += [k for k in kids if k not in marked and k not in siblings]
        if siblings:
            failure, inferred = siblings, True

    conds = {i: cond_of(i) for i in success + failure}
    return success, failure, inferred, conds


def _first_text(graph, start):
    order = [start] + [v for _, v in nx.bfs_edges(graph, start)]
    for n in order:
        if graph.nodes[n].get("has_dialogue"):
            return graph.nodes[n].get("text", "")[:70]
    return ""


def find_active_checks(data: dict, prepared: dict, csv_path: str = "active_skill_checks.csv"):
    # The skill is stored in SkillType as an Articy ID (Actor is who speaks, usually the player)
    articy_to_name = {}
    for a in data.get("actors", []):
        f = {x["title"]: x["value"] for x in a.get("fields", [])}
        articy_to_name[f.get("Articy Id")] = f.get("Name", "?")

    conv_by_id = {c["id"]: c for c in data["conversations"]}

    refs = defaultdict(list)     # variable -> entries that read it in their conditionsString
    for conv in data["conversations"]:
        for e in conv.get("dialogueEntries", []):
            for v in set(VAR_REF_RE.findall(e.get("conditionsString", "") or "")):
                refs[v].append((conv["id"], e["id"]))

    rows = []
    for ideology, p in prepared.items():
        master, targets = p["master"], p["target_vars"]
        setters = {n: _setter_names(d["script"], targets) for n, d in master.nodes(data=True)
                   if d.get("script") and _setter_names(d["script"], targets)}

        for cid in p["conv_ids"]:
            entries = {e["id"]: e for e in conv_by_id[cid]["dialogueEntries"]}
            for e in entries.values():
                f = {x["title"]: x["value"] for x in e.get("fields", [])}
                color = "White" if "DifficultyWhite" in f else "Red" if "DifficultyRed" in f else None
                if color is None:
                    continue

                flag = f.get("FlagName", "")
                success, failure, inferred, conds = find_branches(entries, cid, e["id"], flag)

                check = _key(cid, e["id"])
                succ = [_key(cid, i) for i in success]
                fail = [_key(cid, i) for i in failure]

                # Reachability without being able to "retake" the check: the check node and the opposite branch
                # are hidden, otherwise a hub leading back to the check would distort the result.
                g_fail = nx.restricted_view(master, [check] + succ, [])
                g_succ = nx.restricted_view(master, [check] + fail, [])
                reach_fail, reach_succ = set(), set()
                for n in fail:
                    if n in g_fail:
                        reach_fail |= nx.descendants(g_fail, n) | {n}
                for n in succ:
                    if n in g_succ:
                        reach_succ |= nx.descendants(g_succ, n) | {n}

                # The advanced tasks are compared, not the nodes: two different routes to the same
                # FinishTask are not a loss of progression.
                s_fail = set().union(*(setters[n] for n in reach_fail if n in setters))
                s_succ = set().union(*(setters[n] for n in reach_succ if n in setters))
                lost = s_succ - s_fail
                exits = any("kind" not in master.nodes[n] for n in reach_fail if n in master)

                if not success or not failure:
                    code, verdict = "branches_not_found", "branches not found: read manually"
                elif not s_succ and not s_fail:
                    code, verdict = "no_downstream", "no quest progression downstream"
                elif not lost:
                    code, verdict = "no_loss", "no loss of progression"
                else:
                    code = "loses"
                    verdict = f"failure loses access to {len(lost)} task(s): {', '.join(sorted(lost))}"
                    if exits:
                        verdict += " [exits to another conversation: verify]"
                if inferred and success and failure:
                    verdict += " [failure branch INFERRED: verify with --dump]"

                rows.append({
                    "ideology": ideology, "conv": cid, "entry": e["id"], "color": color,
                    "skill": articy_to_name.get(f.get("SkillType"), f.get("SkillType")),
                    "difficulty": f.get(f"Difficulty{color}"),
                    # only the modifiers with a non-zero value (the slots are always 10)
                    "modifiers": sum(1 for k, v in f.items()
                                     if re.fullmatch(r"modifier\d+", k) and str(v).strip() not in ("", "0")),
                    "flag": flag, "text": (f.get("Dialogue Text") or "")[:80],
                    "success_nodes": ";".join(map(str, success)),
                    "failure_nodes": ";".join(map(str, failure)),
                    "failure_inferred": inferred,
                    "branch_conditions": " | ".join(f"{i}: {c[:90]!r}" for i, c in conds.items()),
                    "tasks_success": len(s_succ), "tasks_failure": len(s_fail),
                    "refs_other_convs": len([r for r in refs.get(flag, []) if r[0] != cid]) if flag else 0,
                    "fail_preview": _first_text(g_fail, fail[0]) if fail and fail[0] in g_fail else "",
                    "verdict_code": code, "verdict": verdict,
                })

    # A check in a shared conversation (e.g. 944) appears once per ideology
    owners = defaultdict(list)
    for r in rows:
        owners[(r["conv"], r["entry"])].append(r["ideology"])
    for r in rows:
        r["also_in"] = ";".join(i for i in owners[(r["conv"], r["entry"])] if i != r["ideology"])

    for r in rows:
        shared = f"  (shared with {r['also_in']})" if r["also_in"] else ""
        print(f"\n[{r['ideology']}] conv {r['conv']}, entry {r['entry']}: {r['color']} "
              f"{r['skill']} [{r['difficulty']}], {r['modifiers']} active modifiers{shared}")
        print(f"  \"{r['text']}\"")
        print(f"  flag: {r['flag'] or '(none)'}  | read in other conversations by {r['refs_other_convs']} entries")
        marker = " (inferred)" if r["failure_inferred"] else ""
        print(f"  success: {r['success_nodes'] or '-'}  | failure{marker}: {r['failure_nodes'] or '-'}")
        if r["branch_conditions"]:
            print(f"  branch conditions: {r['branch_conditions']}")
        if r["fail_preview"]:
            print(f"  after the failure: \"{r['fail_preview']}\"")
        print(f"  quest tasks reachable: success {r['tasks_success']}, failure {r['tasks_failure']}")
        print(f"  => {r['verdict']}")

    print("\n=== SUMMARY ===")
    by_group = Counter((r["ideology"], r["color"]) for r in rows)
    for ideology in prepared:
        print(f"  {ideology}: White {by_group[(ideology, 'White')]}, Red {by_group[(ideology, 'Red')]}")
    print(f"  Occurrences: {len(rows)}; distinct checks: {len(owners)}")
    print("  Outcomes:", dict(Counter(r["verdict_code"] for r in rows)))
    print("  Inferred failure branches:", sum(1 for r in rows if r["failure_inferred"]))

    if rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"  Table saved to {csv_path}")
    return rows


def dump_check(data: dict, conv_id: int, entry_id: int, depth: int = 3):
    """Prints the actual structure around a check, to understand how the branches are encoded."""
    conv_by_id = {c["id"]: c for c in data["conversations"]}
    entries = {e["id"]: e for e in conv_by_id[conv_id]["dialogueEntries"]}

    def show(e, indent):
        f = {x["title"]: x["value"] for x in e.get("fields", [])}
        pad = " " * indent
        print(f"{pad}entry {e['id']} | group={e.get('isGroup')} | type={f.get('DialogueEntryType', '-')} "
              f"| falseConditionAction={e.get('falseConditionAction')!r} | priority={e.get('conditionPriority')}")
        print(f"{pad}   cond   = {e.get('conditionsString', '')!r}")
        print(f"{pad}   script = {(e.get('userScript') or '')[:80]!r}")
        print(f"{pad}   text   = {(f.get('Dialogue Text') or '')[:60]!r}")
        print(f"{pad}   links  -> {[l.get('destinationDialogueID') for l in e.get('outgoingLinks', [])]}")

    def walk(eid, d, indent):
        e = entries.get(eid)
        if e is None:
            return
        show(e, indent)
        if d > 0:
            for link in e.get("outgoingLinks", []):
                dst_conv, dst = _link_dest(link, conv_id)
                if dst_conv == conv_id:
                    walk(dst, d - 1, indent + 4)

    print(f"\n######## conv {conv_id}, entry {entry_id} ########")
    walk(entry_id, depth, 0)
    flag = next((x["value"] for x in entries[entry_id]["fields"] if x["title"] == "FlagName"), "")
    print(f"\n-- entries of the conversation that cite the flag {flag!r}:")
    for e in entries.values():
        if flag and flag in (e.get("conditionsString") or ""):
            print(f"   entry {e['id']}: {e['conditionsString']!r}")


# ---------------------------------------------------------------------------
# 4. Execution
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Active checks (White/Red) in the four Political Vision Quests")
    parser.add_argument("json_path", nargs="?", default=DATA_FILE_PATH,
                        help="exported JSON (default: DATA_FILE_PATH from config.py)")
    parser.add_argument("--csv", default="active_skill_checks.csv", help="output CSV file")
    parser.add_argument("--dump", nargs=2, type=int, action="append", metavar=("CONV", "ENTRY"),
                        help="print the structure of a check and exit (repeatable)")
    args = parser.parse_args()
    if not args.json_path:
        parser.error("specify the path to the JSON (config.py not found or without DATA_FILE_PATH)")

    if _FALLBACK_PATTERNS:
        print("[warning] config.py cannot be imported: using local patterns for GainTask/FinishTask. "
              "Check below that the conversations match Table 4.2.")

    data = load_data(args.json_path)

    if args.dump:
        for conv_id, entry_id in args.dump:
            dump_check(data, conv_id, entry_id)
        return

    prepared = build_prepared(data)

    print("=== Check of the subgraphs against Table 4.2 ===")
    for ideology, p in prepared.items():
        ok = p["conv_ids"] == sorted(EXPECTED_CONVS[ideology])
        print(f"  {ideology}: conversations {p['conv_ids']}  ->  {'OK' if ok else 'DIFFERENT FROM TABLE 4.2'}")

    find_active_checks(data, prepared, args.csv)


if __name__ == "__main__":
    main()