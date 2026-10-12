"""core/time_words.py (ported from DailyNews, AI-53) on DN-Video caption shapes.
Run: python3 tests/test_dn_video_time_words.py"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.time_words import fix_time_words  # noqa: E402

UTC = dt.timezone.utc
TWEET = dt.datetime(2026, 10, 12, 1, 30, tzinfo=UTC)     # 21:30 Eastern, Sunday the 11th
failures = 0


def eq(name, got, want):
    global failures
    ok = got == want
    failures += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"\n     got:  {got!r}\n     want: {want!r}"))


eq("a weekday the tweet gives stays",
   fix_time_words("JUST IN - Miller said Sunday that Democrats will block tax cuts.",
                  "\nStephen Miller on Sunday: Democrats will block the tax cuts", TWEET, "https://x.com/a/status/1")[0],
   "JUST IN - Miller said Sunday that Democrats will block tax cuts.")
eq("a weekday neither the tweet nor the video gives is cut",
   fix_time_words("JUST IN - Miller said on Friday that Democrats will block tax cuts.",
                  "\nStephen Miller: Democrats will block the tax cuts", TWEET, "https://x.com/a/status/1")[0],
   "JUST IN - Miller said that Democrats will block tax cuts.")
eq("a day the transcript gives stays",
   fix_time_words("BREAKING - The vote is set for October 14.",
                  "\nWatch this\nwe will vote on October 14 and not a day later", TWEET, "https://x.com/a/status/1")[0],
   "BREAKING - The vote is set for October 14.")
print("\nall ok" if not failures else f"\n{failures} failed")
raise SystemExit(1 if failures else 0)
