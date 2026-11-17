from __future__ import annotations

import pytest

from ulauncher.utils import fuzzy_search
from ulauncher.utils.fuzzy_search import _matchers, _normalize, get_matching_blocks, get_score


def test_normalize() -> None:
    assert _normalize("Virransäästö") == "virransaasto"
    assert _normalize("Éditeur d’image GIMP") == "editeur dimage gimp"  # noqa: RUF001
    assert _normalize("Ögbelgilengen Uyğulamalar") == "ogbelgilengen uygulamalar"
    assert _normalize("Füße") == "fusse"
    assert _normalize("Вычислитель") == "вычислитель"
    assert _normalize("مُحَرِّر") == "محرر"


def test_get_matching_indexes() -> None:
    assert get_matching_blocks("thfima", "Thunar File Manager") == ([(0, "Th"), (7, "Fi"), (12, "Ma")], 6)
    assert get_matching_blocks("fuss", "Fußball") == ([(0, "Fuß")], 4)
    assert get_matching_blocks("gimp", "Éditeur d’image GIMP") == ([(16, "GIMP")], 4)  # noqa: RUF001
    assert get_matching_blocks("sxs", "ß") == ([(0, "ß")], 2)


def test_get_score() -> None:
    assert get_score("calc", "Contacts") < get_score("calc", "LibreOffice Calc")
    assert get_score("pla", "Pycharm") < get_score("pla", "Google Play Music")
    assert get_score("", "LibreOffice Calc") == 0
    assert get_score("0", "LibreOffice Calc") == 0
    assert get_score("straße", "Straße") <= 100


def test_matcher_reuse__same_results_as_fresh_matchers() -> None:
    texts = ["Thunar File Manager", "Fußball", "Éditeur d’image GIMP", "ß", ""]  # noqa: RUF001
    # a query twice in a row reaches a matcher that is already set to it
    queries = ["thfima", "thfima", "fuss", "", "gimp", "sxs", "FILE", "file manager"]

    def run(fresh: bool) -> list[object]:
        results: list[object] = []
        for query in queries:
            for text in texts:
                # without the pair cache every call reaches a matcher
                get_matching_blocks.cache_clear()  # type: ignore[attr-defined]
                if fresh:
                    _matchers.clear()
                results.append((get_matching_blocks(query, text), get_score(query, text)))
        return results

    _matchers.clear()
    assert run(fresh=False) == run(fresh=True)


def test_get_score__same_text_matched_for_another_query_in_between() -> None:
    text = "Thunar File Manager"
    score = get_score("file", text)
    # as the highlighter does when its input differs from the scored query
    assert get_matching_blocks("thfima", text) == ([(0, "Th"), (7, "Fi"), (12, "Ma")], 6)
    get_matching_blocks.cache_clear()  # type: ignore[attr-defined]
    assert get_matching_blocks("file", text) == ([(7, "File")], 4)
    assert get_score("file", text) == score


def test_matchers__stay_within_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fuzzy_search, "_MATCHER_LIMIT", 3)
    _matchers.clear()
    for number in range(10):
        assert get_matching_blocks("limit", f"limit {number}") == ([(0, "limit")], 5)
        assert len(_matchers) <= 3
