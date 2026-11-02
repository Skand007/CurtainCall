"""Match a raw (title, year) from a CSV to a TMDB or MyAnimeList title.

`decide` is a pure function so it can be unit-tested without any API calls.
`match_row` does the searching and saves the result on the ImportRow.
"""

import difflib
import re
import unicodedata

from . import sources
from .models import ImportRow, Title

HINT_MOVIE = "movie"  # live-action list: TMDB films, then TMDB series, then MyAnimeList
HINT_ANIMATED = "animated"  # animation list: MyAnimeList first, then TMDB films, then TMDB series


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text.startswith("the "):
        text = text[4:]
    return text


def candidate_names(candidate):
    names = [candidate.get("name"), candidate.get("original_name")]
    names += (candidate.get("extra") or {}).get("synonyms", [])
    return {normalize(n) for n in names if n}


def year_close(candidate, year, tolerance=1):
    return candidate.get("year") is not None and abs(candidate["year"] - year) <= tolerance


def decide(raw_title, raw_year, candidates):
    """Return (state, chosen_candidate_or_None).

    MATCHED   exactly one sensible candidate
    REVIEW    several plausible candidates, a human has to pick
    NOT_FOUND nothing came back
    """
    if not candidates:
        return ImportRow.NOT_FOUND, None

    wanted = normalize(raw_title)
    exact = [c for c in candidates if wanted in candidate_names(c)]

    if raw_year:
        same_year = [c for c in exact if c.get("year") == raw_year]
        if len(same_year) == 1:
            return ImportRow.MATCHED, same_year[0]
        near_year = [c for c in exact if year_close(c, raw_year)]
        if len(near_year) == 1:
            return ImportRow.MATCHED, near_year[0]
        if not exact:
            # Title spelled a bit differently but the year agrees, e.g. "Life of Pie" vs "Life of Pi".
            close = [
                c
                for c in candidates
                if year_close(c, raw_year, 0)
                and max(difflib.SequenceMatcher(None, wanted, n).ratio() for n in candidate_names(c)) >= 0.85
            ]
            if len(close) == 1:
                return ImportRow.MATCHED, close[0]
        return ImportRow.REVIEW, None

    if len(exact) == 1:
        return ImportRow.MATCHED, exact[0]
    return ImportRow.REVIEW, None


def search_plan(hint):
    tmdb_films = lambda q, y: sources.search_tmdb(q, Title.KIND_MOVIE, y)  # noqa: E731
    tmdb_series = lambda q, y: sources.search_tmdb(q, Title.KIND_TV, y)  # noqa: E731
    mal = lambda q, y: sources.search_mal(q)  # noqa: E731
    if hint == HINT_ANIMATED:
        return [mal, tmdb_films, tmdb_series]
    return [tmdb_films, tmdb_series, mal]


def simpler_query(text):
    """A shorter search for titles the services miss when typed in full.

    'Demon Slayer: Kimetsu no Yaiba – The Hashira Meeting Arc' -> 'Demon Slayer'
    '[Oshi no Ko] Season 2' -> 'Oshi no Ko Season 2', 'Tokyo Ghoul √A' -> 'Tokyo Ghoul A'
    """
    text = re.split(r"\s[–—-]\s|:\s", text, maxsplit=1)[0]
    text = re.sub(r"[\[\]()√★☆♪*!?.]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def run_plan(query, year, hint):
    """Search each source in turn; stop at the first confident match. Returns (state, chosen, all candidates)."""
    all_candidates = []
    for search in search_plan(hint):
        results = search(query, year)
        all_candidates += results
        state, chosen = decide(query, year, results)
        if state == ImportRow.MATCHED:
            return state, chosen, all_candidates
    state, chosen = decide(query, year, all_candidates)
    return state, chosen, all_candidates


def match_row(row, hint):
    state, chosen, candidates = run_plan(row.raw_title, row.raw_year, hint)
    if state == ImportRow.NOT_FOUND:
        shorter = simpler_query(row.raw_title)
        if shorter and shorter.lower() != row.raw_title.lower():
            # The shorter search only collects candidates. They are still judged against the full
            # title, so "Demon Slayer: ... Hashira Meeting Arc" can never match plain "Demon Slayer".
            candidates = []
            for search in search_plan(hint):
                candidates += search(shorter, row.raw_year)
            state, chosen = decide(row.raw_title, row.raw_year, candidates)
    row.candidates = candidates[:12]  # kept even after a match, so the choice can be undone
    row.state = state
    if state == ImportRow.MATCHED:
        row.matched_title = sources.upsert_title(chosen)
    row.save()
    return row
