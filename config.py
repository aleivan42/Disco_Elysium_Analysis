# filters that are used one or more times across the other snippets

import re

DATA_FILE_PATH = "C:/Users/aleca/Desktop/DE_Analysis/Disco Elysium.json" 

# 24 skills who act as the voices in the protagonist's head + 3 extra voices who take the word in dream sequences but are not skills from a mechanical point of view
# used when creating Gephi graphs, allows to distinguish "internal" dialogue nodes.
SKILLS_LIST = [
    'Logic', 'Encyclopedia', 'Rhetoric', 'Drama', 'Conceptualization', 'Visual Calculus',
    'Volition', 'Inland Empire', 'Empathy', 'Authority', 'Suggestion', 'Esprit de Corps',
    'Endurance', 'Pain Threshold', 'Physical Instrument', 'Electrochemistry', 'Half Light', 'Shivers',
    'Hand/Eye Coordination', 'Perception', 'Reaction Speed', 'Savoir Faire', 'Interfacing', 'Composure',
    'Ancient Reptilian Brain', 'Limbic System', 'Spinal Cord'
]
SET_VAR_RE = re.compile(r'SetVariableValue\(\s*"([^"]+)"')
CHECK_VAR_RE = re.compile(r'Variable\["([^"]+)"\]')
TEST_TITLE_PATTERN = re.compile(r'\b(test|debug|scratch|wip)\b', re.IGNORECASE)
CHECK_VAR_THRESHOLD_RE = re.compile(r'Variable\["([^"]+)"\]\s*(>=|<=|==|>|<)\s*(-?\d+)')
REPUTATION_GROWS_RE = re.compile(r'ReputationGrows\(\s*"([^"]+)"\s*\)')
ISTHC_PRESENT_RE = re.compile(r'IsTHCPresent\(\s*"([^"]+)"\s*\)')
IS_TASK_ACTIVE_RE = re.compile(r'IsTaskActive\(\s*"([^"]+)"\s*\)')
GAIN_TASK_RE = re.compile(r'GainTask\(\s*"([^"]+)"\s*\)')
FINISH_TASK_RE = re.compile(r'FinishTask\(\s*"([^"]+)"\s*\)')

IDEOLOGY_SEED_TASKS = {
    "Communism": "get_yourself_organised",
    "Fascism": "find_a_way_to_turn_back_time",
    "Moralism": "take_on_responsibility",
    "Ultraliberalism": "become_man_of_plenty",
}

""" Reputation variable.* incremented by ReputationGrows for each
    ideology. Fascism is the exception: the
    variable takes its name from its Thought and not from the name direct ideology."""
IDEOLOGY_REPUTATION_VARS = {
    "Communism": "reputation.communist",
    "Fascism": "reputation.revacholian_nationhood",
    "Moralism": "reputation.moralist",
    "Ultraliberalism": "reputation.ultraliberal",
}
