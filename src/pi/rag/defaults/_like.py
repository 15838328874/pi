"""LIKE-pattern escaping for the SQL last-resort fallback (P1).

``search_text`` builds ``LIKE '%<query>%'`` from caller-supplied query text, so
a literal ``%`` / ``_`` / ``\\`` in the query would otherwise act as a wildcard
and silently widen the match set (hit amplification, not wrong answers, but it
reports as clean results instead of a degraded/empty hit). This helper makes
LIKE match the query SUBSTRING literally.

The three metacharacters are escaped (in this order):

* ``\\`` -> ``\\\\``   (must come FIRST: escaping later characters may emit
  backslashes that must themselves be literal)
* ``%``  -> ``\\%``
* ``_``  -> ``\\_``

The caller decides whether it also depends on the backend's default escape
character:

* MySQL      - backslash IS the default LIKE escape char (``NO_BACKSLASH_ESCAPES``
  is off), so the escaped pattern is enough; no ``ESCAPE`` clause needed.
* SQLite     - backslash is NOT special by default; the ``ESCAPE '\\'`` clause
  MUST be added to the SQL for the escapes to take effect.
"""
from __future__ import annotations


def escape_like(term: str) -> str:
    """Return ``term`` with LIKE metacharacters escaped as literals.

    Pure function of its input; safe for both MySQL (default backslash escape)
    and SQLite (pair with an explicit ``ESCAPE '\\'`` clause).
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")