"""Smoke tests for the backup shell scripts.

We invoke the actual scripts with a temp DB and snapshot directory to verify
they produce a real, restorable SQLite snapshot and that the failure paths
exit non-zero. Skipped on platforms where ``sqlite3`` CLI isn't on PATH (it
should be everywhere we deploy, but tests shouldn't fail on a barebones CI).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from squatchtodo import db, repository

REPO_ROOT = Path(__file__).resolve().parent.parent
HOURLY = REPO_ROOT / "deploy" / "backup-hourly.sh"
DAILY = REPO_ROOT / "deploy" / "backup-daily.sh"
WEEKLY = REPO_ROOT / "deploy" / "backup-weekly.sh"


pytestmark = pytest.mark.skipif(
    shutil.which("sqlite3") is None or shutil.which("bash") is None,
    reason="needs sqlite3 + bash on PATH",
)


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "squatch.db"
    db.run_migrations(db_path)
    conn = db.connect(db_path)
    p = repository.create_project(
        conn, name="SNH", tags=["fleet"], created_by="ellie"
    )
    t = repository.create_todo(
        conn, project_id=p.id, title="Fix JSON parsing", created_by="halo"
    )
    repository.add_note(
        conn, todo_id=t.id, content="tried Brotli streaming", author="halo"
    )
    conn.close()
    return db_path


def _run(script: Path, env: dict, expect_ok: bool = True) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **env, "HOME": "/tmp"}
    result = subprocess.run(
        ["bash", str(script)],
        env=full_env,
        capture_output=True,
        text=True,
    )
    if expect_ok and result.returncode != 0:
        pytest.fail(
            f"{script.name} failed: rc={result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def test_hourly_produces_restorable_snapshot(tmp_path: Path, seeded_db: Path):
    snap_dir = tmp_path / "snapshots"
    _run(
        HOURLY,
        env={
            "DB_PATH": str(seeded_db),
            "SNAPSHOT_DIR": str(snap_dir),
            "HOURLY_RETENTION": "5",
        },
    )
    snapshots = sorted(snap_dir.glob("squatchtodo-*.db"))
    assert len(snapshots) == 1

    # Open the snapshot directly and verify the seeded data round-trips.
    conn = sqlite3.connect(snapshots[0])
    conn.row_factory = sqlite3.Row
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok"
    projects = conn.execute("SELECT name, created_by FROM projects").fetchall()
    assert [(r["name"], r["created_by"]) for r in projects] == [("SNH", "ellie")]
    notes = conn.execute("SELECT content, author FROM notes").fetchall()
    assert [(r["content"], r["author"]) for r in notes] == [
        ("tried Brotli streaming", "halo")
    ]
    conn.close()


def test_hourly_prunes_to_retention(tmp_path: Path, seeded_db: Path):
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()

    # Forge five older snapshots with stepped mtimes so ``ls -1t`` ordering is
    # deterministic. Use a 1h step so the time gap is unambiguous.
    for hours_ago in (5, 4, 3, 2, 1):
        forged = snap_dir / f"squatchtodo-fake-{hours_ago:02d}.db"
        forged.write_bytes(b"old")
        mtime = forged.stat().st_mtime - hours_ago * 3600
        os.utime(forged, (mtime, mtime))

    _run(
        HOURLY,
        env={
            "DB_PATH": str(seeded_db),
            "SNAPSHOT_DIR": str(snap_dir),
            "HOURLY_RETENTION": "3",
        },
    )

    surviving = sorted(snap_dir.glob("squatchtodo-*.db"), key=lambda p: p.stat().st_mtime)
    # Retention=3: the 3 newest should survive (the 1h-ago, 2h-ago, plus the
    # just-created real snapshot). The 5h-ago + 4h-ago + 3h-ago + 2h-ago...
    # wait, with retention=3 we keep 3 newest:
    #   - the freshly-created snapshot (mtime=now)
    #   - the 1h-ago forgery
    #   - the 2h-ago forgery
    assert len(surviving) == 3, [p.name for p in surviving]
    names = {p.name for p in surviving}
    assert "squatchtodo-fake-01.db" in names
    assert "squatchtodo-fake-02.db" in names
    assert any(n.startswith("squatchtodo-2") for n in names)  # ISO date-prefixed real one


def test_daily_fails_without_target(tmp_path: Path):
    result = _run(
        DAILY,
        env={"SNAPSHOT_DIR": str(tmp_path), "NAS_TARGET": ""},
        expect_ok=False,
    )
    assert result.returncode != 0
    assert "NAS_TARGET not configured" in result.stderr


def test_weekly_fails_without_target(tmp_path: Path):
    result = _run(
        WEEKLY,
        env={"SNAPSHOT_DIR": str(tmp_path), "DATTO_TARGET": ""},
        expect_ok=False,
    )
    assert result.returncode != 0
    assert "DATTO_TARGET not configured" in result.stderr


def test_daily_fails_when_snapshot_dir_missing(tmp_path: Path):
    result = _run(
        DAILY,
        env={
            "SNAPSHOT_DIR": str(tmp_path / "nope"),
            "NAS_TARGET": "user@nas:/backups",
        },
        expect_ok=False,
    )
    assert result.returncode != 0
    assert "snapshot directory" in result.stderr


def test_hourly_fails_loudly_on_missing_db(tmp_path: Path):
    result = _run(
        HOURLY,
        env={
            "DB_PATH": str(tmp_path / "missing.db"),
            "SNAPSHOT_DIR": str(tmp_path / "snapshots"),
        },
        expect_ok=False,
    )
    assert result.returncode != 0
    assert "database not found" in result.stderr
