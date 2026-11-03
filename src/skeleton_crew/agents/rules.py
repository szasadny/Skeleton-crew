from importlib.resources import files

from skeleton_crew.domain.settings import RULE_KEYS


def default_rules(key: str) -> str:
    if key not in RULE_KEYS:
        raise ValueError(f"unknown rules key: {key}")
    return files("skeleton_crew.agents").joinpath("rules", f"{key}.md").read_text(encoding="utf-8")


def compose_rules(common: str, role_rules: str) -> str:
    return f"{common.strip()}\n\n{role_rules.strip()}\n"
