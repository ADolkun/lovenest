import os
import zlib


def pytest_collection_modifyitems(config, items):
    """Keep one slice of the suite when CI splits it across jobs (PYTEST_SHARD=i/n)."""
    spec = os.environ.get("PYTEST_SHARD")
    if not spec:
        return
    index, total = (int(part) for part in spec.split("/"))
    if not 1 <= index <= total:
        raise ValueError(f"PYTEST_SHARD must be i/n with 1 <= i <= n, got {spec!r}")
    kept, dropped = [], []
    for item in items:
        # crc32, not hash(): hash() is salted per process and every xdist worker must pick the same slice.
        (kept if zlib.crc32(item.nodeid.encode()) % total == index - 1 else dropped).append(item)
    config.hook.pytest_deselected(items=dropped)
    items[:] = kept
