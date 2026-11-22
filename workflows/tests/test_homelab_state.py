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


def test_load_tells_absent_from_unreadable(brain_db):
    assert brain_db.homelab_load("nope") == (None, False)
    brain_db._conn.execute("INSERT INTO homelab_state VALUES ('bad', 'not json', 'x')")
    brain_db._conn.execute("INSERT INTO homelab_state VALUES ('list', '[1]', 'x')")
    brain_db.homelab_put("ok", {"a": 1})
    assert brain_db.homelab_load("bad") == (None, True)
    assert brain_db.homelab_load("list") == (None, True)
    assert brain_db.homelab_load("ok") == ({"a": 1}, False)


def test_put_many_is_all_or_nothing(brain_db):
    brain_db.homelab_put_many({"a": {"n": 1}, "b": {"n": 2}})
    assert brain_db.homelab_get("a") == {"n": 1} and brain_db.homelab_get("b") == {
        "n": 2
    }

    class Unserialisable:
        pass

    try:
        brain_db.homelab_put_many({"a": {"n": 9}, "b": {"n": Unserialisable()}})
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError")
    assert brain_db.homelab_get("a") == {"n": 1}
