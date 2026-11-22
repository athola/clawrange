"""The homelab_state table on the brain database."""


def test_roundtrip_and_overwrite(brain_db):
    assert brain_db.homelab_get("watch") is None
    brain_db.homelab_put("watch", {"a": 1})
    assert brain_db.homelab_get("watch") == {"a": 1}
    brain_db.homelab_put("watch", {"b": [1, 2]})
    assert brain_db.homelab_get("watch") == {"b": [1, 2]}
    assert brain_db.homelab_get("other") is None


def test_unreadable_value_reads_as_none(brain_db):
    brain_db._conn.execute("INSERT INTO homelab_state VALUES ('bad', 'not json', 'x')")
    brain_db._conn.execute("INSERT INTO homelab_state VALUES ('list', '[1]', 'x')")
    assert brain_db.homelab_get("bad") is None
    assert brain_db.homelab_get("list") is None


def test_state_is_never_a_brain_page(brain_db):
    brain_db.homelab_put("watch", {"known_macs": {"00:00:5E:00:53:01": "x"}})
    assert brain_db.list_pages() == []


def test_table_survives_a_second_init(brain_db):
    brain_db.homelab_put("watch", {"a": 1})
    brain_db.init_db()
    assert brain_db.homelab_get("watch") == {"a": 1}
