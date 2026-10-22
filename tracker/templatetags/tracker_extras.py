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
def tilt(index):
    """Fan angle and lift for the nth poster in the homepage stack (0-based)."""
    angles = [-9, -4.5, 0, 4.5, 9, 13]
    lifts = [14, 4, 0, 4, 14, 26]
    i = min(int(index), len(angles) - 1)
    return f"--tilt: {angles[i]}deg; --lift: {lifts[i]}px"
