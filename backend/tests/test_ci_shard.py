import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "shard_conftest", Path(__file__).parents[1] / "conftest.py"
)
assert _SPEC and _SPEC.loader
shard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(shard)


def _select(spec: str | None, nodeids: list[str], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    if spec is None:
        monkeypatch.delenv("PYTEST_SHARD", raising=False)
    else:
        monkeypatch.setenv("PYTEST_SHARD", spec)
    items = [SimpleNamespace(nodeid=nodeid) for nodeid in nodeids]
    deselected: list[object] = []
    config = SimpleNamespace(
        hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items))
    )
    shard.pytest_collection_modifyitems(config, items)
    assert len(items) + len(deselected) == len(nodeids)
    return [item.nodeid for item in items]


NODEIDS = [f"tests/test_module_{n % 7}.py::test_case[{n}]" for n in range(200)]


@pytest.mark.parametrize("total", [1, 2, 3, 5])
def test_shards_partition_the_suite(total: int, monkeypatch: pytest.MonkeyPatch):
    slices = [_select(f"{i}/{total}", NODEIDS, monkeypatch) for i in range(1, total + 1)]
    assert sorted(nodeid for part in slices for nodeid in part) == sorted(NODEIDS)
    assert all(slices)


def test_unset_keeps_everything(monkeypatch: pytest.MonkeyPatch):
    assert _select(None, NODEIDS, monkeypatch) == NODEIDS


@pytest.mark.parametrize("spec", ["0/3", "4/3", "x", "1/0"])
def test_a_bad_spec_fails_loudly(spec: str, monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError):
        _select(spec, NODEIDS, monkeypatch)
