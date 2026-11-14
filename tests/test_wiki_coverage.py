import pathlib

import frontmatter

WIKI = pathlib.Path(__file__).resolve().parent.parent / "wiki"
REQUIRED = {
    "experiment": ["experiment-question", "participants", "pilot", "verdict-criteria",
                   "forecasts-and-calibration", "random-bots-baseline", "self-learning"],
    "risk-cage": ["cage-rules", "hp-floor", "stops"],
    "system-architecture": ["tick-and-session", "agent-tools", "accounts", "model-and-budget", "analytics-page"],
    "operations": ["setup-and-keys", "schedule-and-hosting"],
    "results": [],
}

def test_design_is_in_the_wiki():
    for category, leaves in REQUIRED.items():
        assert (WIKI / "categories" / f"{category}.md").exists(), category
        for leaf in leaves:
            meta = frontmatter.load(WIKI / "articles" / f"{leaf}.md").metadata
            assert category in meta["categories"], leaf
