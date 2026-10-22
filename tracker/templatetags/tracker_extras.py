from django import template

register = template.Library()


@register.filter
def stars(rating):
    """4.5 -> '★★★★½'. Empty for no rating."""
    if rating is None or rating == "":
        return ""
    halves = int(round(float(rating) * 2))
    return "★" * (halves // 2) + ("½" if halves % 2 else "")


@register.filter
def money(usd):
    """187_436_818 -> '$187.4 million'; 2_923_706_026 -> '$2.92 billion'."""
    if not usd:
        return ""
    usd = int(usd)
    if usd >= 1_000_000_000:
        return f"${usd / 1_000_000_000:.2f} billion"
    if usd >= 1_000_000:
        return f"${usd / 1_000_000:.1f} million"
    return f"${usd:,}"


@register.filter
def short_count(n):
    """1_234_567 -> '1.2M', 45_300 -> '45K'."""
    if not n:
        return ""
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}K"
    return str(n)


@register.filter
def hours_minutes(minutes):
    """142 -> '2 h 22 min', 45 -> '45 min', 1500 -> '25 h'."""
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    if h == 0:
        return f"{m} min"
    if h >= 10 or m == 0:
        return f"{h} h"
    return f"{h} h {m} min"
