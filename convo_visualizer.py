from pathlib import Path
from pyvis.network import Network
from config import DATA_FILE_PATH
from data_loading import load_game_data
# ---------------------------------------------------------------------------
CONVERSATION_ID = 1198
OUTPUT_HTML = "conv1198_politihub.html"

USE_ARTICY_COORDINATES = True
FLIP_Y = True

# Visibility settings
CANVAS_HEIGHT = "1000px"
NODE_SIZE = 12            # fixed size of every node
SHOW_LABELS = False       # True: print a short label under each node
FONT_SIZE = 16            # label size (only used when SHOW_LABELS is True)
LABEL_LENGTH = 45         # characters shown before a label is truncated
EDGE_WIDTH = 2.5          # links inside the conversation
CROSS_EDGE_WIDTH = 3      # links leading to another conversation
INTRA_EDGE_COLOR = "#cfd8dc"
CROSS_EDGE_COLOR = "#ff5252"
# ---------------------------------------------------------------------------


def get_field(entry: dict, title: str, default=""):
    """Pull a value out of the {title, value, type} fields array."""
    for f in entry.get("fields", []):
        if f.get("title") == title:
            return f.get("value", default)
    return default


def truncate(text: str, n: int = LABEL_LENGTH) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def find_conversation(data: dict, conversation_id: int) -> list[dict]:
    for conv in data.get("conversations", []):
        if conv.get("id") == conversation_id:
            return conv.get("dialogueEntries", [])
    raise SystemExit(f"No conversation with id={conversation_id} found in {DATA_FILE_PATH}")


HIERARCHICAL_OPTIONS = """
{
  "layout": {
    "hierarchical": {
      "enabled": true,
      "direction": "LR",
      "sortMethod": "directed",
      "levelSeparation": 260,
      "nodeSpacing": 160,
      "treeSpacing": 220,
      "blockShifting": true,
      "edgeMinimization": true,
      "parentCentralization": true
    }
  },
  "physics": {"enabled": false},
  "edges": {
    "width": 2.5,
    "hoverWidth": 1.5,
    "selectionWidth": 2,
    "arrows": {"to": {"enabled": true, "scaleFactor": 1.0}},
    "smooth": {"type": "cubicBezier", "forceDirection": "horizontal", "roundness": 0.4}
  },
  "interaction": {"hover": true, "navigationButtons": true, "keyboard": true, "dragNodes": true}
}
"""

ARTICY_COORDINATES_OPTIONS = """
{
  "layout": {"hierarchical": {"enabled": false}},
  "physics": {"enabled": false},
  "edges": {
    "width": 2.5,
    "hoverWidth": 1.5,
    "selectionWidth": 2,
    "arrows": {"to": {"enabled": true, "scaleFactor": 1.0}},
    "smooth": {"type": "cubicBezier", "roundness": 0.2}
  },
  "interaction": {"hover": true, "navigationButtons": true, "keyboard": true, "dragNodes": true}
}
"""


def edge_color(hex_color: str) -> dict:
    """Fully opaque edge colour; links turn yellow on hover or selection."""
    return {"color": hex_color, "highlight": "#ffeb3b", "hover": "#ffeb3b",
            "inherit": False, "opacity": 1.0}


def build_graph(entries: list[dict]) -> Network:
    net = Network(
        height=CANVAS_HEIGHT,
        width="100%",
        directed=True,
        bgcolor="#111111",
        font_color="white",
        notebook=False,
    )
    net.set_options(ARTICY_COORDINATES_OPTIONS if USE_ARTICY_COORDINATES else HIERARCHICAL_OPTIONS)

    entries_by_id = {e["id"]: e for e in entries if "id" in e}
    known_ids = set(entries_by_id)

    # Out-degree per node, computed up front, so branching/choice points
    # can be marked visually instead of blending in with linear dialogue.
    out_degree = {eid: len(entry.get("outgoingLinks", [])) for eid, entry in entries_by_id.items()}

    seen_stub_ids = set()

    for eid, entry in entries_by_id.items():
        dialogue_text = get_field(entry, "Dialogue Text", "")
        title = get_field(entry, "Title", f"Entry {eid}")
        actor = get_field(entry, "Actor", "")
        conditions = entry.get("conditionsString", "") or ""
        script = entry.get("userScript", "") or ""

        is_narrative = bool(dialogue_text.strip())
        is_branch = out_degree.get(eid, 0) >= 2
        label = truncate(dialogue_text if is_narrative else title)

        color = "#7fb3ff" if is_narrative else "#9a9a9a"
        border = "#ff9800" if conditions.strip() else color

        # Plain text: recent vis-network versions do not render HTML in tooltips.
        # Line breaks and wrapping are handled by the CSS injected below.
        tooltip_lines = [
            f"Entry {eid} (conv {entry.get('conversationID', '?')})",
            f"Actor: {actor}",
            f"Text: {dialogue_text}" if is_narrative else "[structural node]",
        ]
        if is_branch:
            tooltip_lines.append(f"Branch point: {out_degree[eid]} outgoing options")
        if conditions.strip():
            tooltip_lines.append(f"Condition: {conditions}")
        if script.strip():
            tooltip_lines.append(f"Script: {script}")

        node_kwargs = dict(
            label=label if SHOW_LABELS else " ",
            title="\n".join(tooltip_lines),
            color={"background": color, "border": border},
            font={"size": FONT_SIZE, "color": "#e0e0e0"},
            shape="diamond" if is_branch else "square",
            size=NODE_SIZE + 4 if is_branch else NODE_SIZE,
            borderWidth=4 if conditions.strip() else 1,
        )

        if USE_ARTICY_COORDINATES:
            rect = entry.get("canvasRect", {})
            x = rect.get("x", 0.0)
            y = rect.get("y", 0.0)
            node_kwargs["x"] = x
            node_kwargs["y"] = -y if FLIP_Y else y
            node_kwargs["physics"] = False

        net.add_node(eid, **node_kwargs)

    for eid, entry in entries_by_id.items():
        for link in entry.get("outgoingLinks", []):
            dest = link.get("destinationDialogueID")
            dest_conv = link.get("destinationConversationID")
            if dest is None:
                continue

            if dest not in known_ids:
                stub_id = f"ext_{dest_conv}_{dest}"
                if stub_id not in seen_stub_ids:
                    stub_kwargs = dict(
                        label=f"→ conv {dest_conv} #{dest}" if SHOW_LABELS else " ",
                        title=f"External reference\nconversation {dest_conv}, entry {dest}",
                        color={"background": "#2b2b2b", "border": CROSS_EDGE_COLOR},
                        font={"size": FONT_SIZE, "color": "#e0e0e0"},
                        shape="square",
                        size=NODE_SIZE,
                        borderWidth=3,
                    )
                    if USE_ARTICY_COORDINATES:
                        rect = entry.get("canvasRect", {})
                        base_x = rect.get("x", 0.0)
                        base_y = rect.get("y", 0.0)
                        stub_kwargs["x"] = base_x + 200
                        stub_kwargs["y"] = (-base_y if FLIP_Y else base_y) + 60
                        stub_kwargs["physics"] = False
                    net.add_node(stub_id, **stub_kwargs)
                    seen_stub_ids.add(stub_id)
                net.add_edge(eid, stub_id, color=edge_color(CROSS_EDGE_COLOR),
                             width=CROSS_EDGE_WIDTH, dashes=True)
            else:
                cross = dest_conv is not None and dest_conv != entry.get("conversationID")
                if cross:
                    net.add_edge(eid, dest, color=edge_color(CROSS_EDGE_COLOR),
                                 width=CROSS_EDGE_WIDTH, dashes=True)
                else:
                    net.add_edge(eid, dest, color=edge_color(INTRA_EDGE_COLOR),
                                 width=EDGE_WIDTH)

    return net


SEARCH_AND_LEGEND_HTML = """
<style>
  div.vis-tooltip {
    max-width: 520px !important;
    white-space: pre-wrap !important;
    overflow-wrap: anywhere !important;
    word-wrap: break-word !important;
    font-size: 13px !important;
    line-height: 1.4 !important;
    padding: 8px 10px !important;
  }
</style>
<div style="position:fixed; top:10px; left:10px; z-index:1000; background:#1c1c1c;
            border:1px solid #444; border-radius:8px; padding:10px 14px;
            font-family:sans-serif; color:white; font-size:13px;">
  <input id="node-search" type="text" placeholder="Search to highlight nodes..."
         style="width:220px; padding:4px 6px; border-radius:4px; border:1px solid #555;
                background:#222; color:white;">
  <div style="margin-top:8px; line-height:1.6;">
    <div><span style="display:inline-block;width:12px;height:12px;background:#7fb3ff;margin-right:6px;"></span>Narrative node</div>
    <div><span style="display:inline-block;width:12px;height:12px;background:#9a9a9a;margin-right:6px;"></span>Structural node</div>
    <div><span style="display:inline-block;width:12px;height:12px;border:2px solid #ff9800;margin-right:6px;"></span>Conditional node (has a condition)</div>
    <div><span style="display:inline-block;width:10px;height:10px;transform:rotate(45deg);border:2px solid #cfd8dc;margin-right:8px;"></span>Choice point (diamond, &ge;2 options)</div>
    <div><span style="display:inline-block;width:12px;height:12px;background:#2b2b2b;border:2px solid #ff5252;margin-right:6px;"></span>External reference</div>
    <div><span style="display:inline-block;width:22px;height:0;border-top:3px solid #cfd8dc;margin-right:6px;vertical-align:middle;"></span>Link within the conversation</div>
    <div><span style="display:inline-block;width:22px;height:0;border-top:3px dashed #ff5252;margin-right:6px;vertical-align:middle;"></span>Link to another conversation</div>
    <div style="margin-top:6px; color:#aaa;">Hover over a node to read its text</div>
  </div>
</div>
<script>
network.fit();
</script>
<script>
var originalNodeStates = {};
nodes.get().forEach(function (n) {
  originalNodeStates[n.id] = JSON.parse(JSON.stringify(n));
});

document.getElementById("node-search").addEventListener("input", function (e) {
  var term = e.target.value.toLowerCase();
  var updates = [];
  nodes.get().forEach(function (n) {
    var orig = originalNodeStates[n.id];
    var haystack = ((n.label || "") + " " + (n.title || "")).toLowerCase();
    var match = term !== "" && haystack.indexOf(term) !== -1;
    if (match) {
      updates.push({
        id: n.id,
        color: { background: "#ffeb3b", border: "#ffffff" },
        borderWidth: 4
      });
    } else {
      updates.push({ id: n.id, color: orig.color, borderWidth: orig.borderWidth });
    }
  });
  nodes.update(updates);
});
</script>
"""


def inject_search_and_legend(html_path: str) -> None:
    html = Path(html_path).read_text(encoding="utf-8")
    html = html.replace("</body>", SEARCH_AND_LEGEND_HTML + "</body>")
    Path(html_path).write_text(html, encoding="utf-8")


def main():
    data = load_game_data()
    entries = find_conversation(data, CONVERSATION_ID)
    if not entries:
        raise SystemExit(f"Conversation {CONVERSATION_ID} has no dialogue entries.")

    net = build_graph(entries)
    net.write_html(OUTPUT_HTML, open_browser=False, notebook=False)
    inject_search_and_legend(OUTPUT_HTML)
    print(f"Conversation {CONVERSATION_ID}: wrote {len(entries)} entries -> {OUTPUT_HTML}")


if __name__ == "__main__":
    main()