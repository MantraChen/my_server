"""Read the reviewed upstream pin without evaluating repository data as shell code."""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def read_source(path=None):
    source = json.loads((path or Path(__file__).with_name("source.json")).read_text())
    if set(source) != {"repository", "commit", "site", "base"}:
        raise ValueError("source.json must contain repository, commit, site and base")
    if source["repository"] != "MantraChen/Dev-Blog":
        raise ValueError("unexpected upstream repository")
    if not isinstance(source["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
        raise ValueError("upstream commit must be a full lowercase SHA")
    if not isinstance(source["site"], str) or not re.fullmatch(r"https://[a-z0-9.-]+", source["site"]):
        raise ValueError("site must be an HTTPS origin without a path, port or credentials")
    if not urlsplit(source["site"]).hostname:
        raise ValueError("site must contain a hostname")
    if not isinstance(source["base"], str) or not re.fullmatch(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", source["base"]):
        raise ValueError("base must be an absolute subpath without a trailing slash")
    return source


if __name__ == "__main__":
    source = read_source()
    if sys.argv[1:] == ["--github-output"]:
        print(f"sha={source['commit']}\nsite={source['site']}\nbase={source['base']}")
    elif not sys.argv[1:]:
        print(source["commit"])
    else:
        sys.exit("usage: source.py [--github-output]")
