# loads the file, usable separately to verify that the file has been loaded properly. 
# the loading is to be considered successful if 424 actors get loaded

import json
from config import DATA_FILE_PATH

def load_game_data(file_path: str = DATA_FILE_PATH) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)

def build_actor_map(data: dict) -> dict:
    actors_list = data["actors"]
    actor_map = {}
    for actor in actors_list:
        name = next((f["value"] for f in actor["fields"] if f["title"] == "Name"), "Unknown")
        actor_map[str(actor["id"])] = name
    return actor_map

if __name__ == "__main__":
    game_data = load_game_data()
    actor_map = build_actor_map(game_data)
    print(f"Loaded {len(actor_map)} actors!")
