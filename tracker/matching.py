"""Match a raw (title, year) from a CSV to a TMDB or AniList title.

`decide` is a pure function so it can be unit-tested without any API calls.
`match_row` does the searching and saves the result on the ImportRow.
"""

import difflib
import re
import unicodedata

from . import sources
from .models import ImportRow, Title

HINT_MOVIE = "movie"  # live-action list: TMDB films first, then series
HINT_ANIMATED = "animated"  # animation list: AniList first, then TMDB films, then series


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
    if hint == HINT_ANIMATED:
        return [
            lambda q, y: sources.search_anilist(q),
            lambda q, y: sources.search_tmdb(q, Title.KIND_MOVIE, y),
            lambda q, y: sources.search_tmdb(q, Title.KIND_TV, y),
        ]
    return [
        lambda q, y: sources.search_tmdb(q, Title.KIND_MOVIE, y),
        lambda q, y: sources.search_tmdb(q, Title.KIND_TV, y),
    ]


def match_row(row, hint):
    """Search each source in turn; stop at the first confident match."""
    all_candidates = []
    for search in search_plan(hint):
        results = search(row.raw_title, row.raw_year)
        all_candidates += results
        state, chosen = decide(row.raw_title, row.raw_year, results)
        if state == ImportRow.MATCHED:
            row.state = ImportRow.MATCHED
            row.matched_title = sources.upsert_title(chosen)
            row.candidates = []
            row.save()
            return row

    state, chosen = decide(row.raw_title, row.raw_year, all_candidates)
    if state == ImportRow.MATCHED:
        row.matched_title = sources.upsert_title(chosen)
        row.candidates = []
    else:
        # Keep the top few from every source for the review screen.
        row.candidates = all_candidates[:12]
    row.state = state
    row.save()
    return row
