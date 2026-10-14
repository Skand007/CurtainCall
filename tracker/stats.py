"""Library statistics for the stats page."""

from collections import Counter

from .models import Entry, Title


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

    ratings = Counter(float(e.rating) for e in entries if e.rating is not None)
    rating_labels = [x / 2 for x in range(1, 11)]

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
        "ratings": {"labels": rating_labels, "values": [ratings.get(r, 0) for r in rating_labels]},
    }
