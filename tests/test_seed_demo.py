"""seed_demo loads the lead-crm demo CSV into the configured CRM database.

Run with: pytest tests/test_seed_demo.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

import seed_demo
from crm import get_adapter


@pytest.fixture
def crm_db(tmp_path, monkeypatch):
    """Point the lead-crm profile's SQLite CRM at a throwaway file."""
    path = tmp_path / "crm.db"
    monkeypatch.setenv("CRM_DB_PATH", str(path))
    return path


def _leads(path):
    crm = get_adapter({"adapter": "sqlite", "sqlite": {"path": str(path)}})
    return crm.list("leads", limit=1000)


class TestSeedDemo:
    def test_main_loads_demo_leads_into_crm_db_path(self, crm_db, capsys):
        """GIVEN CRM_DB_PATH pointing at a new database
        WHEN seed_demo runs
        THEN it exits 0, reports the database and the demo leads are stored."""
        assert seed_demo.main() == 0
        out = capsys.readouterr().out
        assert "seed-demo: wrote" in out and str(crm_db) in out
        emails = {row["email"] for row in _leads(crm_db)}
        assert "ada@example.com" in emails and "grace@example.com" in emails

    def test_rerun_upserts_instead_of_duplicating(self, crm_db):
        """GIVEN a database already seeded once
        WHEN seed_demo runs again
        THEN the lead count is unchanged."""
        seed_demo.main()
        first = len(_leads(crm_db))
        assert seed_demo.main() == 0
        assert len(_leads(crm_db)) == first > 0

    def test_report_counts_match_stored_rows(self, crm_db, capsys):
        """GIVEN the demo CSV
        WHEN seed_demo runs
        THEN the 'wrote N leads' figure equals the rows in the database."""
        seed_demo.main()
        out = capsys.readouterr().out
        written = int(out.split("wrote ")[1].split(" leads")[0])
        assert written == len(_leads(crm_db))

    def test_missing_portal_connector_fails_with_message(
        self, crm_db, monkeypatch, capsys
    ):
        """GIVEN a lead-crm profile without a 'portal-leads' connector
        WHEN seed_demo runs
        THEN it returns 1, explains why on stderr and writes nothing."""

        class NoConnector:
            crm = {"adapter": "sqlite"}

            def connector(self, connector_id):
                return None

        monkeypatch.setattr(seed_demo, "load_profile", lambda name: NoConnector())
        assert seed_demo.main() == 1
        assert "no 'portal-leads' connector" in capsys.readouterr().err
        assert not crm_db.exists()

    def test_missing_demo_csv_raises(self, crm_db, monkeypatch, tmp_path):
        """GIVEN the demo CSV is absent from the repo root
        WHEN seed_demo runs
        THEN FileNotFoundError surfaces rather than seeding an empty CRM."""
        monkeypatch.setattr(seed_demo, "_REPO", str(tmp_path))
        with pytest.raises(FileNotFoundError):
            seed_demo.main()
        assert not crm_db.exists()
