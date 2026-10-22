"""Library statistics for the stats page."""

from collections import Counter

from .models import Entry, Title

SCORE_BANDS = [f"{low}–{low + 9}" for low in range(0, 90, 10)] + ["90–100"]


def band_counts(scores):
    """[87, 91, 100, 12] -> counts per 10-point band; 100 joins the 90s."""
    counts = [0] * 10
    for s in scores:
        counts[min(int(s) // 10, 9)] += 1
    return counts


def library_stats(user):
    entries = list(Entry.objects.filter(user=user, status=Entry.STATUS_WATCHED).select_related("title"))
    titles = [e.title for e in entries]

    by_kind = Counter(t.get_kind_display() for t in titles)
    animated = sum(1 for t in titles if t.animated or t.kind == Title.KIND_ANIME)
    minutes = sum(t.runtime_minutes or 0 for t in titles)

    decades = Counter((t.year // 10) * 10 for t in titles if t.year)
    decade_labels = sorted(decades)

    genres = Counter(g for t in titles for g in t.genres)
    directors = Counter(d for t in titles if t.kind == Title.KIND_MOVIE for d in t.directors)
    studios = Counter(d for t in titles if t.kind == Title.KIND_ANIME for d in t.directors)

    # Critics (Rotten Tomatoes, 0-100) against audiences (IMDb users, 0-10 scaled to 0-100), in 10-point bands.
    critics = [t.rt_critics for t in titles if t.rt_critics is not None]
    audience = [round(t.imdb_rating * 10) for t in titles if t.imdb_rating is not None]

    return {
        "total": len(titles),
        "by_kind": dict(by_kind),
        "animated": animated,
        "live_action": len(titles) - animated,
        "hours": round(minutes / 60),
        "missing_runtime": sum(1 for t in titles if not t.runtime_minutes),
        "decades": {"labels": [f"{d}s" for d in decade_labels], "values": [decades[d] for d in decade_labels]},
        "genres": genres.most_common(12),
        "directors": directors.most_common(10),
        "studios": studios.most_common(10),
        "scores": {
            "labels": SCORE_BANDS,
            "critics": band_counts(critics),
            "audience": band_counts(audience),
            "critics_n": len(critics),
            "audience_n": len(audience),
            "critics_avg": round(sum(critics) / len(critics)) if critics else None,
            "audience_avg": round(sum(audience) / len(audience)) if audience else None,
        },
    }
