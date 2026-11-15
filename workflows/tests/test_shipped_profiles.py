"""Every profile shipped in config/profiles must load and validate.

A YAML syntax error in one profile (an invalid escape) once passed the whole
suite because no test loaded that file.
"""

import pathlib

import pytest

from crm.query import find_template, run_query
from crm.sqlite_adapter import SQLiteCRM
from generators import GENERATORS
from tenant_profile import load_profile

PROFILES = pathlib.Path(__file__).resolve().parents[2] / "config" / "profiles"
NAMES = sorted(p.parent.name for p in PROFILES.glob("*/profile.yaml"))


def test_profiles_found():
    assert {"starter", "marketing", "lead-crm"} <= set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_shipped_profile_loads(name):
    p = load_profile(name, profiles_dir=PROFILES, known_generator_kinds=set(GENERATORS))
    assert p.name == name


def test_lead_lookup_treats_wildcards_literally(tmp_path):
    p = load_profile(
        "lead-crm", profiles_dir=PROFILES, known_generator_kinds=set(GENERATORS)
    )
    t = find_template(p.query_templates(), "lead_lookup")
    crm = SQLiteCRM(str(tmp_path / "c.db"))
    crm.init()
    crm.upsert(
        "leads",
        [
            {"name": "50% off sale", "email": "a@x.com"},
            {"name": "Fifty", "email": "b@x.com"},
        ],
        "email",
    )
    names = [r["name"] for r in run_query(crm, t, {"q": "50%"})]
    assert names == ["50% off sale"]
