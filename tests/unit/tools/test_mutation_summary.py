import sqlite3

from tools.mutation_summary import summarize


def test_summarizes_outcomes_and_documents_the_score(tmp_path):
    session = tmp_path / "pure.sqlite"
    with sqlite3.connect(session) as conn:
        conn.execute("CREATE TABLE results (test_outcome TEXT)")
        conn.executemany(
            "INSERT INTO results VALUES (?)", [("killed",), ("killed",), ("survived",)]
        )

    summary = summarize(session)

    assert summary["killed"] == 2
    assert summary["survived"] == 1
    assert summary["mutation_score"] == 2 / 3
