from importlib.resources import files

from skeleton_crew.domain.settings import check_rule_key


def default_rules(key: str) -> str:
    check_rule_key(key)
    return files("skeleton_crew.agents").joinpath("rules", f"{key}.md").read_text(encoding="utf-8")


def effective_rules(key: str, active: str | None) -> str:
    return active or default_rules(key)


def compose_rules(common: str, role_rules: str) -> str:
    return f"{common.strip()}\n\n{role_rules.strip()}\n"
