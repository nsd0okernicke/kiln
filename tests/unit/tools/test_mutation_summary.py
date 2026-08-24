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
    assert summary["behavioral_mutation_score"] == 2 / 3


def test_reports_annotation_mutants_separately(tmp_path, monkeypatch):
    module = tmp_path / "sample.py"
    module.write_text("def parse(value: str | None) -> str:\n    return value or ''\n")
    session = tmp_path / "db.sqlite"
    with sqlite3.connect(session) as conn:
        conn.execute(
            "CREATE TABLE mutation_specs "
            "(job_id TEXT, module_path TEXT, start_pos_row INT, start_pos_col INT)"
        )
        conn.execute(
            "CREATE TABLE work_results "
            "(job_id TEXT, worker_outcome TEXT, test_outcome TEXT)"
        )
        conn.executemany(
            "INSERT INTO mutation_specs VALUES (?, ?, ?, ?)",
            [("annotation", str(module), 1, 17), ("behavior", str(module), 2, 11)],
        )
        conn.executemany(
            "INSERT INTO work_results VALUES (?, ?, ?)",
            [("annotation", "normal", "survived"), ("behavior", "normal", "killed")],
        )

    monkeypatch.chdir(tmp_path)
    summary = summarize(session)

    assert summary["annotation_outcomes"] == {"survived": 1}
    assert summary["behavioral_killed"] == 1
    assert summary["behavioral_survived"] == 0
    assert summary["behavioral_mutation_score"] == 1.0
