"""The write path's configuration file, and its pattern filter on facts stored on purpose (3.3, step 3).

The configuration is `write_path.toml`, beside this module.
"""

import pathlib
import re
import tomllib

CONFIG = pathlib.Path(__file__).with_name("write_path.toml")


def load():
    with CONFIG.open("rb") as file:
        return tomllib.load(file)


def refusal(text):
    """Why `text` is not stored, or None when it passes."""
    config = load()
    stripped = text.strip()
    if len(stripped) < config["min_length"]:
        return f"shorter than {config['min_length']} characters"
    for pattern in config["narration"]:
        if re.search(pattern, stripped, re.IGNORECASE):
            return "narration, not a fact: it says how the agent felt or what it is about to do"
    return None
