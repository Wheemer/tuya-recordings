import json

import pytest

from custom_components.tuya_recordings.lib import storage


def test_catalog_replacement_is_complete_and_leaves_no_temporary_files(tmp_path):
    path = tmp_path / "private" / "catalog.json"
    storage.write_catalog(path, {"clips": [1]})
    storage.write_catalog(path, {"clips": [2, 3]})
    assert json.loads(path.read_text()) == {"clips": [2, 3]}
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_failed_write_preserves_previous_catalog(tmp_path, monkeypatch, failure):
    path = tmp_path / "catalog.json"
    storage.write_catalog(path, {"clips": [1]})
    def fail(*args):
        raise OSError("Synthetic disk failure")
    monkeypatch.setattr(storage.os, failure, fail)
    with pytest.raises(OSError):
        storage.write_catalog(path, {"clips": [2]})
    assert json.loads(path.read_text()) == {"clips": [1]}
    assert list(tmp_path.iterdir()) == [path]


def test_invalid_payload_does_not_damage_previous_catalog(tmp_path):
    path = tmp_path / "catalog.json"
    storage.write_catalog(path, {"clips": [1]})
    with pytest.raises(TypeError):
        storage.write_catalog(path, {"invalid": object()})
    assert json.loads(path.read_text()) == {"clips": [1]}
