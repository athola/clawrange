"""Pure parsers for the homelab sources. Synthetic data only."""

from datetime import UTC, datetime

from homelab.models import Device, EntityState, KevEntry, Monitor
from homelab.parse import (
    devices_from_census,
    entities_from_states,
    kev_entries,
    monitors_from_kuma,
    parse_mgmt_ports,
    parse_routeros_tables,
    parse_system_resource,
    router_version,
    safe_label,
)

# ─── RouterOS tables ────────────────────────────────────────────────

LEASES = (
    "Flags: X - disabled, R - radius, D - dynamic, B - blocked\r\n"
    "Columns: ADDRESS, MAC-ADDRESS, HOST-NAME\r\n"
    " #    ADDRESS       MAC-ADDRESS        HOST-NAME     \r\n"
    " 0    192.0.2.10    00:00:5E:00:53:01  printer-1     \r\n"
    " 1 D  192.0.2.11    02:AA:BB:CC:DD:01                \r\n"
    "57 DC 192.0.2.12    00:00:5E:00:53:03  tv_living*room\r\n"
)

ARP = (
    "Flags: X - disabled, I - invalid, H - DHCP, D - dynamic, P - published\r\n"
    "Columns: ADDRESS, MAC-ADDRESS, INTERFACE, STATUS\r\n"
    " #   ADDRESS      MAC-ADDRESS        INTERFACE  STATUS   \r\n"
    " 0 D 192.0.2.10   00:00:5E:00:53:01  bridge     reachable\r\n"
    " 1 D 192.0.2.11   02:AA:BB:CC:DD:01  bridge     stale    \r\n"
    " 2 D 198.51.100.9 00:00:5E:00:53:09  ether3     reachable\r\n"
    " 3 D 192.0.2.99                      bridge     failed   \r\n"
)


def test_tables_split_and_flags_and_two_digit_index():
    tables = parse_routeros_tables(LEASES + ARP)
    assert len(tables) == 2
    leases, arp = tables
    assert [r["#"] for r in leases] == ["0", "1", "57"]
    assert [r["flags"] for r in leases] == ["", "D", "DC"]
    assert leases[2]["ADDRESS"] == "192.0.2.12"
    assert leases[2]["HOST-NAME"] == "tv_living*room"
    assert len(arp) == 4


def test_blank_hostname_cell_is_empty_string():
    leases = parse_routeros_tables(LEASES)[0]
    assert leases[1]["HOST-NAME"] == ""
    assert leases[1]["MAC-ADDRESS"] == "02:AA:BB:CC:DD:01"


def test_flags_absent_single_flag_column_width():
    raw = (
        "Columns: ADDRESS, MAC-ADDRESS, HOST-NAME\n"
        " #  ADDRESS     MAC-ADDRESS        HOST-NAME\n"
        " 0  192.0.2.10  00:00:5E:00:53:01  printer-1\n"
        " 1  192.0.2.11  00:00:5E:00:53:02  nas-1\n"
    )
    (rows,) = parse_routeros_tables(raw)
    assert [r["HOST-NAME"] for r in rows] == ["printer-1", "nas-1"]
    assert all(r["flags"] == "" for r in rows)


def test_crlf_and_trailing_whitespace_do_not_leak_into_cells():
    rows = parse_routeros_tables(LEASES)[0]
    assert rows[0]["HOST-NAME"] == "printer-1"
    assert not any("\r" in v for r in rows for v in r.values())
    assert not any(v != v.strip() for r in rows for v in r.values())


def test_value_wider_than_header_falls_back_to_whitespace_split():
    raw = (
        " #   ADDRESS MAC-ADDRESS        HOST-NAME\n"
        " 0   198.51.100.200 00:00:5E:00:53:05 printer-1\n"
    )
    (rows,) = parse_routeros_tables(raw)
    assert rows[0]["ADDRESS"] == "198.51.100.200"
    assert rows[0]["MAC-ADDRESS"] == "00:00:5E:00:53:05"
    assert rows[0]["HOST-NAME"] == "printer-1"


def test_empty_and_garbage_input():
    assert parse_routeros_tables("") == []
    assert parse_routeros_tables("not a table\nat all\n") == []
    assert parse_routeros_tables(" #   ADDRESS\n") == [[]]


def test_devices_merge_lease_and_arp_by_mac():
    devices = {d.mac: d for d in devices_from_census(LEASES + ARP)}
    printer = devices["00:00:5E:00:53:01"]
    assert printer == Device(
        mac="00:00:5E:00:53:01",
        ip="192.0.2.10",
        hostname="printer-1",
        interface="bridge",
        status="reachable",
        in_lease=True,
    )
    assert devices["02:AA:BB:CC:DD:01"].randomized
    assert devices["02:AA:BB:CC:DD:01"].hostname == ""
    # ARP-only device: not in the lease table
    assert devices["00:00:5E:00:53:09"].in_lease is False
    assert devices["00:00:5E:00:53:09"].ip == "198.51.100.9"
    # lease-only device keeps its lease address, no interface
    assert devices["00:00:5E:00:53:03"].interface == ""
    assert devices["00:00:5E:00:53:03"].in_lease is True
    assert devices["00:00:5E:00:53:03"].ip == "192.0.2.12"


def test_devices_skip_rows_without_valid_mac_and_are_sorted():
    devices = devices_from_census(LEASES + ARP)
    assert all(d.ip != "192.0.2.99" for d in devices)
    macs = [d.mac for d in devices]
    assert macs == sorted(macs)
    assert devices_from_census("") == ()


def test_devices_uppercase_macs():
    raw = (
        " #   ADDRESS     MAC-ADDRESS        INTERFACE\n"
        " 0   192.0.2.5   00:00:5e:00:53:0a  bridge\n"
    )
    (device,) = devices_from_census(raw)
    assert device.mac == "00:00:5E:00:53:0A"


# ─── system resource, ports, KEV ────────────────────────────────────


def test_system_resource_and_version():
    raw = "uptime: 1w2d\r\nversion: 7.1.2 (stable)\r\nboard-name: hEX S\r\n"
    parsed = parse_system_resource(raw)
    assert parsed["version"] == "7.1.2 (stable)"
    assert parsed["board-name"] == "hEX S"
    assert router_version(raw) == "7.1.2 (stable)"
    assert router_version("uptime: 1d\n") is None
    assert router_version("") is None


def test_mgmt_ports_sorted_unique():
    scan = {"open": ["192.0.2.1 -> [8291,22,53,22]"]}
    assert parse_mgmt_ports(scan) == (22, 53, 8291)


def test_mgmt_ports_empty_open_list_is_valid():
    assert parse_mgmt_ports({"open": []}) == ()


def test_mgmt_ports_malformed_is_none():
    assert parse_mgmt_ports({}) is None
    assert parse_mgmt_ports({"open": "22"}) is None
    assert parse_mgmt_ports({"open": ["192.0.2.1 22 53"]}) is None
    assert parse_mgmt_ports({"open": ["192.0.2.1 -> [22,99999]"]}) is None
    assert parse_mgmt_ports("nope") is None


KEV_ROWS = [
    {
        "cveID": "CVE-2000-0001",
        "vendorProject": "MikroTik",
        "product": "RouterOS",
        "vulnerabilityName": "RouterOS test flaw",
        "dateAdded": "2024-01-02",
        "shortDescription": "Synthetic entry.",
    },
    {
        "cveID": "CVE-2000-0002",
        "vendorProject": "mikrotik",
        "product": "Winbox",
        "vulnerabilityName": "Winbox test flaw",
        "dateAdded": "2024-02-03",
        "shortDescription": "Another synthetic entry.",
    },
    {
        "cveID": "CVE-2000-0003",
        "vendorProject": "Example Corp",
        "product": "Widget",
        "vulnerabilityName": "Not ours",
        "dateAdded": "2024-03-04",
        "shortDescription": "Other vendor.",
    },
]


def test_kev_from_full_feed_filters_mikrotik_case_insensitively():
    entries = kev_entries({"vulnerabilities": KEV_ROWS})
    assert [e.cve_id for e in entries] == ["CVE-2000-0001", "CVE-2000-0002"]
    assert entries[0] == KevEntry(
        "CVE-2000-0001",
        "2024-01-02",
        "RouterOS",
        "RouterOS test flaw",
        "Synthetic entry.",
    )


def test_kev_from_list_and_malformed():
    assert len(kev_entries(KEV_ROWS)) == 2
    assert kev_entries({"vulnerabilities": "x"}) == ()
    assert kev_entries([{"vendorProject": "MikroTik"}, "junk", 3]) == ()
    assert kev_entries({}) == ()


# ─── Home Assistant ─────────────────────────────────────────────────


def test_entities_from_states():
    states = [
        {
            "entity_id": "sensor.hall_battery",
            "state": "15",
            "attributes": {
                "friendly_name": "Hall sensor battery",
                "device_class": "battery",
                "unit_of_measurement": "%",
            },
            "last_changed": "2026-01-02T03:04:05+00:00",
        },
        {"entity_id": "light.lamp", "state": "on", "attributes": {}},
        {"entity_id": "switch.x", "state": "off", "attributes": None},
        {"state": "on"},
        "junk",
    ]
    entities = entities_from_states(states)
    assert len(entities) == 3
    first = entities[0]
    assert first == EntityState(
        "sensor.hall_battery",
        "15",
        "Hall sensor battery",
        "battery",
        "%",
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    assert first.domain == "sensor"
    assert entities[1].name == "light.lamp"  # falls back to the id
    assert entities[1].last_changed is None


def test_entities_zulu_timestamp_and_bad_timestamp():
    ents = entities_from_states(
        [
            {"entity_id": "a.b", "state": "on", "last_changed": "2026-01-02T03:04:05Z"},
            {"entity_id": "a.c", "state": "on", "last_changed": "garbage"},
        ]
    )
    assert ents[0].last_changed == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert ents[1].last_changed is None


# ─── Uptime Kuma ────────────────────────────────────────────────────


def test_monitors_from_kuma():
    page = {
        "publicGroupList": [
            {
                "name": "g1",
                "monitorList": [{"id": 1, "name": "Router", "type": "ping"}],
            },
            {"name": "g2", "monitorList": [{"id": 2, "name": "NAS"}]},
        ]
    }
    heartbeat = {
        "heartbeatList": {
            "1": [{"status": 1, "time": "t0"}, {"status": 0, "time": "t1"}],
            "2": [],
        },
        "uptimeList": {"1_24": 0.987},
    }
    monitors = monitors_from_kuma(page, heartbeat)
    assert monitors == (
        Monitor("1", "Router", 0, 0.987),
        Monitor("2", "NAS", None, None),
    )


def test_monitors_malformed_input():
    assert monitors_from_kuma({}, {}) == ()
    assert monitors_from_kuma({"publicGroupList": "x"}, {}) == ()
    page = {"publicGroupList": [{"monitorList": [{"id": 3, "name": "A"}]}]}
    assert monitors_from_kuma(page, {"heartbeatList": []})[0].status is None


# ─── safe_label ─────────────────────────────────────────────────────


def test_safe_label_escapes_markdown_specials():
    assert safe_label("tv_living*room") == "tv\\_living\\*room"
    assert safe_label("a`b[c") == "a\\`b\\[c"


def test_safe_label_strips_controls_and_collapses_whitespace():
    assert safe_label("a\x00b\x1b[0m\tc\n\r d") == "ab\\[0m c d"
    assert safe_label("  padded   name  ") == "padded name"


def test_safe_label_caps_length_with_ellipsis():
    out = safe_label("x" * 100, limit=10)
    assert out == "x" * 9 + "…"
    assert safe_label("short", limit=10) == "short"


def test_safe_label_non_string_and_empty():
    assert safe_label("") == ""
    assert safe_label(None) == ""


def test_kev_entries_accepts_scanner_file_shape():
    doc = {
        "when": "2026-01-01T00:00:00+00:00",
        "entries": [
            {"cveID": "CVE-2000-0002", "vendorProject": " MikroTik ", "dateAdded": "d"},
            {"cveID": "CVE-2000-0003", "vendorProject": "Other"},
        ],
    }
    assert [e.cve_id for e in kev_entries(doc)] == ["CVE-2000-0002"]
    assert kev_entries({"when": "x", "entries": "nope"}) == ()
