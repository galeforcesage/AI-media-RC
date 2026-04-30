#!/usr/bin/env python3
"""Diagnose dateparser behavior for problematic phrases."""
from dateparser.search import search_dates
from datetime import datetime

phrases = [
    "what recorded last night",
    "last night",
    "what recorded yesterday",
    "yesterday",
    "tonight",
    "what records tonight",
    "last Monday",
    "this past week",
]

settings_past = {"PREFER_DATES_FROM": "past", "RETURN_AS_TIMEZONE_AWARE": False, "DATE_ORDER": "MDY"}
settings_future = {"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False, "DATE_ORDER": "MDY"}

print(f"Now: {datetime.now()}")
print()

for phrase in phrases:
    r_past = search_dates(phrase, settings=settings_past, languages=["en"])
    r_future = search_dates(phrase, settings=settings_future, languages=["en"])
    print(f"Phrase: {phrase!r}")
    print(f"  past:   {r_past}")
    print(f"  future: {r_future}")
    print()
