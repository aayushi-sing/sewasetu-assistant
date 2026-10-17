# -*- coding: utf-8 -*-
"""Regression: a new application number must never equal an existing one.
Run from agent/:  PORTAL_PATH=../app python tests/test_application_no.py"""
import os, sys
sys.path.insert(0, os.environ.get("PORTAL_PATH", "/app"))
import app as portal

conn = portal.get_db(); cur = conn.cursor()
cur.execute("SELECT application_no FROM applications WHERE application_no LIKE %s LIMIT 1",
            ("SSP" + __import__("datetime").datetime.now().strftime("%y") + "%",))
existing = cur.fetchone()[0]
suffix = int(existing[5:])

# force the random draw to hit an existing number first, then a free one
draws = iter([suffix, 100000 if suffix != 100000 else 100001])
portal.random.randint = lambda a, b: next(draws)
got = portal.new_application_no(cur)
assert got != existing, "generator returned an existing application number"
cur.execute("SELECT count(*) FROM applications WHERE application_no=%s", (got,))
assert cur.fetchone()[0] == 0
print("PASS: a colliding draw is skipped (%s skipped, %s used)" % (existing, got))

cur.execute("SELECT count(*) - count(DISTINCT application_no) FROM applications")
assert cur.fetchone()[0] == 0
print("PASS: no duplicate application numbers in the database")