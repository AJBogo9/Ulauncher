"""Check that the fuzzy scorer in the working tree returns exactly what a git revision's returns.

Usage (from the repo root, with the app's python):

    python scripts/perf/check_scoring.py [--base REV]

get_score and get_matching_blocks of both versions run on every searchable field of the installed
apps (plus a few unicode and long texts) against every prefix, up to 12 chars, of app names, random
strings, word pairs and unicode queries. The pairs that differ are counted, and the count must be 0.

Two planted controls run on the same pairs to show that the comparison can fail: a scorer with
another word-boundary penalty, and a matcher that never moves on from its first query.

The app list comes from the environment (XDG_DATA_DIRS, XDG_DATA_HOME, XDG_CURRENT_DESKTOP).
Loading the settings can rewrite settings.json, so point XDG_CONFIG_HOME at a copy.
REV defaults to HEAD, which is only a comparison while the change is uncommitted.
About two million pairs with 100 apps, which takes a bit over a minute.
"""

from __future__ import annotations

import argparse
import importlib.util
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
SOURCE = "ulauncher/utils/fuzzy_search.py"
MAX_PREFIX = 12
RANDOM_QUERIES = 100
WORD_PAIR_QUERIES = 60

UNICODE_TEXTS = [
    "Motörhead",
    "Straße",
    "ß",
    "ﬁrefox",
    "Fußball",
    "Éditeur d’image GIMP",  # noqa: RUF001
    "Cafe\u0301 Cre\u0300me",  # combining accents
    "Virransäästö",
    "İstanbul Kahvesi",
    "Вычислитель",
    "مُحَرِّر",
    "日本語 入力",
]
UNICODE_QUERIES = [
    *UNICODE_TEXTS,
    "motorhead",
    "strasse",
    "STRASSE",
    "sxs",
    "firefox",
    "fussball",
    "editeur d'image",
    "cafe\u0301",
    "café creme",
    "e\u0301\u0301e",
    "istanbul",
    "вычисл",
    "محرر",
    "“gimp”",
]

sys.path.insert(0, str(REPO))


def load_module(name: str, source: str) -> ModuleType:
    """Import `source` as a module of its own, with its own caches."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.py"
        path.write_text(source)
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def planted_penalty(source: str) -> ModuleType:
    """Control: the same scorer with another word-boundary penalty."""
    penalty = "0.5 / query_len"
    if source.count(penalty) != 1:
        msg = f"expected {penalty!r} once in {SOURCE}"
        raise RuntimeError(msg)
    return load_module("fuzzy_search_penalty", source.replace(penalty, "0.4 / query_len"))


def planted_stale_query(source: str) -> ModuleType:
    """Control: every text stays matched against its first query, as with a reused matcher that ignores new ones."""
    module = load_module("fuzzy_search_stale", source)
    fresh = module.get_matching_blocks
    first_query: dict[str, str] = {}

    def stale(query: str, text: str) -> tuple[list[tuple[int, str]], int]:
        return fresh(first_query.setdefault(text, query), text)

    # get_score looks get_matching_blocks up in its module, so it sees the stale blocks too
    module.get_matching_blocks = stale
    return module


def app_fields() -> tuple[list[str], list[str]]:
    """Names and searchable fields of the installed apps."""
    from ulauncher.modes.apps.app_mode import AppMode

    triggers = list(AppMode().get_triggers())
    fields = {field for trigger in triggers for field, _ in trigger.get_searchable_fields()}
    return [trigger.name for trigger in triggers], sorted(fields)


def prefixes(names: list[str], fields: list[str]) -> list[str]:
    rng = random.Random(0)  # noqa: S311
    letters = "abcdefghijklmnopqrstuvwxyz"
    words = sorted({word for field in fields for word in field.lower().split()})
    queries = [
        *names,
        *("".join(rng.choices(letters, k=MAX_PREFIX)) for _ in range(RANDOM_QUERIES)),
        *(" ".join(rng.sample(words, 2)) for _ in range(WORD_PAIR_QUERIES)),
        *UNICODE_QUERIES,
    ]
    # from length 0, so the empty query is in too
    return sorted({query[:length] for query in queries for length in range(MAX_PREFIX + 1)})


def count_mismatches(
    base: ModuleType, candidates: dict[str, ModuleType], queries: list[str], texts: list[str]
) -> dict[str, list[int]]:
    """Per candidate, the number of pairs where the score and where the blocks differ from base."""
    counts = {name: [0, 0] for name in candidates}
    for query in queries:
        # all texts per query, as a keystroke does, so that every matcher is reused for every query
        for text in texts:
            # repr tells 100 from 100.0 and 0.0 from -0.0, and is exact for floats
            score = repr(base.get_score(query, text))
            blocks = base.get_matching_blocks(query, text)
            for name, module in candidates.items():
                counts[name][0] += repr(module.get_score(query, text)) != score
                counts[name][1] += module.get_matching_blocks(query, text) != blocks
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="HEAD", help="git revision with the reference implementation")
    args = parser.parse_args()

    base_source = subprocess.run(
        ["git", "show", f"{args.base}:{SOURCE}"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout
    tree_source = (REPO / SOURCE).read_text()
    if base_source == tree_source:
        print(f"note: {SOURCE} is the same in {args.base} and the working tree, pass an older --base")

    names, fields = app_fields()
    if not fields:
        print("no apps found, check XDG_DATA_DIRS and XDG_CURRENT_DESKTOP")
        return 1
    joined = " ".join(fields)
    # past 200 chars SequenceMatcher drops the most common chars from its index
    texts = [*fields, *UNICODE_TEXTS, joined[:250], joined[:1000]]
    queries = prefixes(names, fields)

    counts = count_mismatches(
        load_module("fuzzy_search_base", base_source),
        {
            "working tree": load_module("fuzzy_search_tree", tree_source),
            "planted penalty": planted_penalty(tree_source),
            "planted stale query": planted_stale_query(tree_source),
        },
        queries,
        texts,
    )

    print(f"apps {len(names)}, texts {len(texts)}, query prefixes {len(queries)}")
    print(f"pairs compared: {len(queries) * len(texts)}")
    for name, (scores, blocks) in counts.items():
        print(f"{name + ':':21} {scores:>8} score mismatches, {blocks:>8} block mismatches")

    tree_ok = counts["working tree"] == [0, 0]
    controls_ok = counts["planted penalty"][0] > 0 and counts["planted stale query"][1] > 0
    if not controls_ok:
        print("FAIL: a planted control went unnoticed")
    print("identical" if tree_ok else "FAIL: the working tree scores differently")
    return 0 if tree_ok and controls_ok else 1


if __name__ == "__main__":
    sys.exit(main())
