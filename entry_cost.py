import json
import re
import pandas as pd
from config import (
    CHECK_VAR_THRESHOLD_RE,
    DATA_FILE_PATH,
    IDEOLOGY_REPUTATION_VARS,
    ISTHC_PRESENT_RE,
    REPUTATION_GROWS_RE,
)

AFTERTHOUGHT_THRESHOLD_RE = re.compile(
    r"(?:reputation\.)?(communist|revacholian_nationhood|fascist|moralist|ultraliberal)\s*(>=|>|==)\s*([0-9]+)",
    re.IGNORECASE
)

def reputation_grows_counts(data: dict) -> pd.DataFrame:
   
    bare_name_to_ideology = {}
    for ideology, var_name in IDEOLOGY_REPUTATION_VARS.items():
        bare = var_name.split(".", 1)[1]
        bare_name_to_ideology[bare] = ideology
    
    bare_name_to_ideology.update({
        "communist": "Communism",
        "fascist": "Fascism",
        "revacholian_nationhood": "Fascism",
        "ultraliberal": "Ultraliberalism",
        "moralist": "Moralism"
    })

    entries_by_ideology = {ideology: set() for ideology in IDEOLOGY_REPUTATION_VARS}
    convs_by_ideology = {ideology: set() for ideology in IDEOLOGY_REPUTATION_VARS}

    for conv in data.get("conversations", []):
        conv_id = conv["id"]
        for entry in conv.get("dialogueEntries", []):
            uscript = entry.get("userScript", "") or ""
            for bare_name in REPUTATION_GROWS_RE.findall(uscript):
                ideology = bare_name_to_ideology.get(bare_name)
                if ideology is None:
                    continue
                entries_by_ideology[ideology].add((conv_id, entry["id"]))
                convs_by_ideology[ideology].add(conv_id)

    rows = []
    for ideology, var_name in IDEOLOGY_REPUTATION_VARS.items():
        n_entries = len(entries_by_ideology[ideology])
        n_convs = len(convs_by_ideology[ideology])
        density = round(n_entries / n_convs, 2) if n_convs > 0 else 0.0

        rows.append({
            "Ideology": ideology,
            "Variable": var_name,
            "Increment_Entries": n_entries,
            "Distinct_Conversations": n_convs,
            "Density_Ratio": density
        })

    df = pd.DataFrame(rows)

    print("\n" + "="*70)
    print("REPUTATION INCREMENT DISTRIBUTION")
    print("="*70)
    print(df.to_string(index=False))
    print("="*70)

    return df


def threshold_checks(data: dict) -> dict[str, list[tuple[int, int, str, int]]]:
    print("\n" + "="*70)
    print("NUMERICAL THRESHOLD CHECKS ON REPUTATION COUNTERS:")
    print("="*70)

    results = {}
    for ideology, variable_name in IDEOLOGY_REPUTATION_VARS.items():
        hits = []
        for conv in data.get("conversations", []):
            conv_id = conv["id"]
            for entry in conv.get("dialogueEntries", []):
                cond = entry.get("conditionsString", "") or ""
                for var, op, value in CHECK_VAR_THRESHOLD_RE.findall(cond):
                    if var == variable_name:
                        hits.append((conv_id, entry["id"], op, int(value)))
        
        results[ideology] = hits
        print(f" -> {ideology:<15} ({variable_name}): {len(hits)} threshold check(s) found.")
        for conv_id, entry_id, op, value in hits:
            print(f"    [Conv {conv_id:<4} | Entry {entry_id:<5}] Condition: {variable_name} {op} {value}")

    print("="*70)
    return results



def run_entry_cost_analysis(data: dict):
    """Executes the full Step 2 analysis pipeline."""
    df_distribution = reputation_grows_counts(data)
    threshold_checks(data)


if __name__ == "__main__":
    print(f"Loading dialogue data from {DATA_FILE_PATH}...")
    with open(DATA_FILE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    run_entry_cost_analysis(data)