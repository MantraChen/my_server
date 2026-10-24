"""Read the reviewed upstream pin without evaluating repository data as shell code."""
import json
import re
from pathlib import Path


def read_source(path=None):
    source = json.loads((path or Path(__file__).with_name("source.json")).read_text())
    if set(source) != {"repository", "commit"}:
        raise ValueError("source.json must contain only repository and commit")
    if source["repository"] != "MantraChen/Dev-Blog":
        raise ValueError("unexpected upstream repository")
    if not isinstance(source["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
        raise ValueError("upstream commit must be a full lowercase SHA")
    return source


if __name__ == "__main__":
    print(read_source()["commit"])
