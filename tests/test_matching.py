from tracker.matching import decide, normalize
from tracker.models import ImportRow

from .conftest import cand


def test_normalize_handles_accents_ampersands_and_articles():
    assert normalize("Amélie") == "amelie"
    assert normalize("Pain & Gain") == normalize("Pain and Gain")
    assert normalize("The Batman") == "batman"
    assert normalize("Mission: Impossible – Fallout") == "mission impossible fallout"


def test_exact_title_and_year_matches():
    state, chosen = decide("Heat", 1995, [cand("Heat", 1995), cand("Heat", 2013)])
    assert state == ImportRow.MATCHED
    assert chosen["year"] == 1995


def test_year_off_by_one_still_matches():
    state, chosen = decide("Boss Level", 2020, [cand("Boss Level", 2021)])
    assert state == ImportRow.MATCHED


def test_remake_without_year_needs_review():
    state, chosen = decide("Aladdin", None, [cand("Aladdin", 1992), cand("Aladdin", 2019)])
    assert state == ImportRow.REVIEW
    assert chosen is None


def test_remake_with_year_picks_the_right_one():
    state, chosen = decide("Aladdin", 2019, [cand("Aladdin", 1992), cand("Aladdin", 2019)])
    assert state == ImportRow.MATCHED
    assert chosen["year"] == 2019


def test_single_exact_without_year_matches():
    state, chosen = decide("Heat", None, [cand("Heat", 1995), cand("Heat Wave", 2022)])
    assert state == ImportRow.MATCHED


def test_typo_matches_when_year_agrees():
    state, chosen = decide("Life of Pie", 2012, [cand("Life of Pi", 2012), cand("Pie in the Sky", 1996)])
    assert state == ImportRow.MATCHED
    assert chosen["name"] == "Life of Pi"


def test_wrong_year_goes_to_review():
    state, _ = decide("Heat", 1980, [cand("Heat", 1995)])
    assert state == ImportRow.REVIEW


def test_anime_matches_on_romaji_or_synonym():
    anime = cand(
        "Spirited Away",
        2001,
        source="anilist",
        kind="anime",
        original="Sen to Chihiro no Kamikakushi",
        synonyms=["Chihiro"],
    )
    assert decide("Sen to Chihiro no Kamikakushi", None, [anime])[0] == ImportRow.MATCHED
    assert decide("Chihiro", None, [anime])[0] == ImportRow.MATCHED


def test_no_candidates_is_not_found():
    assert decide("Nothing", 2000, []) == (ImportRow.NOT_FOUND, None)
