"""The canonical catalog seed data, pinned by the requirements.

Every scenario that seeds the catalog uses these three books, verbatim. The values are not
arbitrary: they are chosen so that a filter term matches at most one book. Titles and genres
share no common substring, and authors share none of three characters or more — so an author
filter must use at least three characters ("her", "fow", "tol"), since "er" and "rt" each match
two of them.

Substituting other values re-opens the ambiguity these were chosen to close. Genres like
"Science Fiction" and "Computer Science" both contain "Science", which makes an Examples row
expecting a total of 1 unsatisfiable under case-insensitive substring matching — and the
contradiction surfaces only as a failing acceptance test that the coder cannot fix, because the
feature file is specifier-owned.

Scaffolded by Kiln. Import these constants rather than repeating the literals; the Background
table in a feature file must match them exactly.
"""

from __future__ import annotations

from typing import NamedTuple


class SeedBook(NamedTuple):
    """One pre-seeded catalog book."""

    isbn: str
    title: str
    author: str
    genre: str


#: The three seeded books, in title-ascending order — the catalog's default sort.
SEED_BOOKS: tuple[SeedBook, ...] = (
    SeedBook("978-0-20-163361-0", "Dune", "Frank Herbert", "Sci-Fi"),
    SeedBook("978-0-13-468599-1", "Refactoring", "Martin Fowler", "Software"),
    SeedBook("978-3-16-148410-0", "The Hobbit", "J.R.R. Tolkien", "Fantasy"),
)
