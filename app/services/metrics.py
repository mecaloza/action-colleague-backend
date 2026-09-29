"""Small numeric helpers shared by the learner and admin views."""


def percentage(part: int, whole: int) -> float:
    """`part` as a percentage of `whole`, rounded to one decimal; 0.0 when there is nothing to divide by."""
    return round(part / whole * 100, 1) if whole else 0.0
