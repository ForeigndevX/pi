"""
Swedish public holidays 2024–2030 (Europe/Stockholm calendar dates).
Fixed dates + computus (Easter) + Midsummer / All Saints rules.
"""
from __future__ import annotations

from datetime import date, timedelta

HOLIDAY_YEAR_MIN = 2024
HOLIDAY_YEAR_MAX = 2030

# (month, day) fixed holidays — name_sv, name_en
_FIXED = (
    (1, 1, "Nyårsdagen", "New Year's Day"),
    (1, 6, "Trettondedag jul", "Epiphany"),
    (5, 1, "Första maj", "May Day"),
    (6, 6, "Nationaldagen", "National Day"),
    (12, 25, "Juldagen", "Christmas Day"),
    (12, 26, "Annandag jul", "Boxing Day"),
)

# Optional display (not all lists count as full public holiday)
_MIDSOMMAR_AFTON = True


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Western Easter)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _midsummer_day(year: int) -> date:
    """Midsommardagen: Saturday between 20 and 26 June."""
    for d in range(20, 27):
        cand = date(year, 6, d)
        if cand.weekday() == 5:  # Saturday
            return cand
    return date(year, 6, 20)  # fallback (unreachable)


def _midsummer_eve(year: int) -> date:
    return _midsummer_day(year) - timedelta(days=1)


def _all_saints(year: int) -> date:
    """Alla helgons dag: Saturday between 31 Oct and 6 Nov."""
    for d in range(31, 32):
        cand = date(year, 10, d)
        if cand.weekday() == 5:
            return cand
    for d in range(1, 7):
        cand = date(year, 11, d)
        if cand.weekday() == 5:
            return cand
    return date(year, 11, 1)


def holidays_for_year(year: int, include_midsummer_eve: bool = _MIDSOMMAR_AFTON) -> list[dict]:
    if year < HOLIDAY_YEAR_MIN or year > HOLIDAY_YEAR_MAX:
        return []

    out: list[dict] = []

    def add(d: date, name_sv: str, name_en: str, *, public: bool = True):
        out.append({
            "date": d.isoformat(),
            "name_sv": name_sv,
            "name_en": name_en,
            "public": public,
        })

    for month, day, sv, en in _FIXED:
        add(date(year, month, day), sv, en)

    easter = _easter_sunday(year)
    add(easter - timedelta(days=2), "Långfredagen", "Good Friday")
    add(easter, "Påskdagen", "Easter Sunday")
    add(easter + timedelta(days=1), "Annandag påsk", "Easter Monday")
    add(easter + timedelta(days=39), "Kristi himmelsfärds dag", "Ascension Day")
    add(_all_saints(year), "Alla helgons dag", "All Saints' Day")
    if include_midsummer_eve:
        add(_midsummer_eve(year), "Midsommarafton", "Midsummer Eve", public=False)
    add(_midsummer_day(year), "Midsommardagen", "Midsummer Day")

    out.sort(key=lambda h: h["date"])
    return out


def holidays_by_date(year: int) -> dict[str, list[dict]]:
    """Map ISO date -> list of holiday entries (may include eve)."""
    by: dict[str, list[dict]] = {}
    for h in holidays_for_year(year):
        by.setdefault(h["date"], []).append(h)
    return by
