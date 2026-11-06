"""Lossless compact snapshots and backwards-compatible decoding."""

import json
from contextlib import closing

import numpy as np
import pytest

from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
from lab_control.storage.analysis import _snapshot
from lab_control.storage.repository import Repository


@pytest.mark.parametrize("dtype", ["float32", "float64", "int64", "complex128"])
@pytest.mark.parametrize("with_axis", [False, True])
def test_compact_snapshot_preserves_arrays_and_axis_absence(tmp_path, dtype, with_axis):
    values = np.array([1, 2, 3], dtype=dtype)
    if dtype == "complex128":
        values += 2j
    axis = np.array([0.2, 0.4, 0.6], dtype="float32") if with_axis else None
    batch = MeasurementBatch(
        DeviceIdentity("Test", "Scope", "SERIAL"),
        (Measurement("voltage", "V", values, axis=axis, axis_unit="s" if with_axis else ""),),
        configuration={"range": 3},
        timing={"clock_source": "test"},
    )
    with closing(Repository(tmp_path)) as repository:
        folder = repository.export(batch)
    raw = folder / "_raw"
    document = json.loads((raw / "snapshot.json").read_text(encoding="utf-8"))
    assert "values" not in document["measurements"][0]
    for npz_only in (False, True):
        if npz_only:
            (raw / "snapshot.json").unlink()
        restored = _snapshot(raw)
        sample = restored["measurements"][0]
        np.testing.assert_array_equal(sample["values"], values)
        assert sample["values"].dtype == values.dtype
        assert restored["configuration"] == batch.configuration
        assert restored["timestamp"] == batch.timestamp
        if with_axis:
            np.testing.assert_array_equal(sample["axis"], axis)
            assert sample["axis"].dtype == axis.dtype
        else:
            assert sample["axis"] is None
    assert list(folder.glob("*.csv"))


def test_missing_npz_is_not_a_complete_snapshot(tmp_path):
    (tmp_path / "snapshot.json").write_text(json.dumps({"snapshot_format_version": 2, "measurements": []}))
    with pytest.raises(ValueError, match="archive is missing"):
        _snapshot(tmp_path)


def test_legacy_full_json_reads_without_archive(tmp_path):
    document = {"measurements": [{"values": [1.25, 2.5], "axis": None}]}
    (tmp_path / "snapshot.json").write_text(json.dumps(document))
    restored = _snapshot(tmp_path)
    np.testing.assert_array_equal(restored["measurements"][0]["values"], [1.25, 2.5])
    assert restored["measurements"][0]["axis"] is None
