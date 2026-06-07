"""Connector pipeline — chain source -> transform -> sink (FR-3.4).

``run_connector`` resolves each stage's kind from the registries, runs them
in order against the supplied CRM adapter, and returns
``{fetched, kept, written}`` counts. ``http_client`` is injectable so the
source stage can be driven by an ``httpx.MockTransport`` in tests.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("clawrange.connectors.pipeline")


def run_connector(spec: dict, crm, *, http_client=None) -> dict:
    from . import SINKS, SOURCES, TRANSFORMS

    src_spec = spec.get("source") or {}
    source = SOURCES.get(src_spec.get("kind"))
    if source is None:
        raise ValueError(f"unknown source kind {src_spec.get('kind')!r}")
    rows = source(src_spec, client=http_client)
    fetched = len(rows)

    tf_spec = spec.get("transform")
    if tf_spec:
        transform = TRANSFORMS.get(tf_spec.get("kind"))
        if transform is None:
            raise ValueError(f"unknown transform kind {tf_spec.get('kind')!r}")
        rows = transform(rows, tf_spec)
    kept = len(rows)

    sink_spec = spec.get("sink") or {}
    sink = SINKS.get(sink_spec.get("kind"))
    if sink is None:
        raise ValueError(f"unknown sink kind {sink_spec.get('kind')!r}")
    written = sink(rows, sink_spec, crm)

    counts = {"fetched": fetched, "kept": kept, "written": written}
    logger.info("run_connector %s -> %s", spec.get("id", "?"), counts)
    return counts
