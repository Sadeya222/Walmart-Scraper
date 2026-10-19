"""Atomic writes and the 'never overwrite good data' publication gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from walmart_scraper.atomic_io import (
    append_jsonl,
    atomic_write_text,
    publish_dataset,
    read_jsonl,
    sha256_file,
)


def test_atomic_write_creates_parents_and_content(tmp_path):
    target = tmp_path / "nested" / "dir" / "out.txt"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    atomic_write_text(tmp_path / "out.txt", "hello")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.txt"]


def test_atomic_write_is_all_or_nothing(tmp_path, monkeypatch):
    """A failure during promotion leaves the previous file and no temp litter."""
    target = tmp_path / "out.txt"
    atomic_write_text(target, "first")

    def explode(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr("walmart_scraper.atomic_io.os.replace", explode)
    with pytest.raises(OSError):
        atomic_write_text(target, "second")

    assert target.read_text() == "first"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.txt"]


def test_append_jsonl_and_read_round_trip(tmp_path):
    path = tmp_path / "rows.jsonl"
    append_jsonl(path, [{"a": 1}, {"b": 2}])
    append_jsonl(path, [{"c": 3}])
    assert list(read_jsonl(path)) == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_read_jsonl_skips_a_truncated_trailing_line(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"a":1}\n{"b":2\n{"c":3}\n', encoding="utf-8")
    assert list(read_jsonl(path)) == [{"a": 1}, {"c": 3}]


def test_sha256_file(tmp_path):
    path = tmp_path / "x.txt"
    path.write_text("abc", encoding="utf-8")
    assert sha256_file(path).startswith("ba7816bf")
    assert sha256_file(tmp_path / "missing") == ""


def _seeded_dataset(tmp_path: Path) -> Path:
    dataset = tmp_path / "walmart_catalog.jsonl"
    append_jsonl(dataset, [{"zipcode": "78745", "product_title": f"good {i}"} for i in range(50)])
    return dataset


def test_thin_run_is_withheld_and_previous_dataset_survives(tmp_path):
    dataset = _seeded_dataset(tmp_path)
    before = dataset.read_text()
    staged = tmp_path / "staged.jsonl"
    append_jsonl(staged, [{"product_title": "only one row"}])

    stats = publish_dataset(
        staged, dataset, record_count=1, min_records=25, quarantine_dir=tmp_path / "quarantine"
    )

    assert stats.published is False
    assert "below_minimum_records_1<25" in stats.withheld_reason
    assert dataset.read_text() == before, "the previous dataset must not be touched"
    quarantined = sorted(p.name for p in (tmp_path / "quarantine").iterdir())
    assert any(name.endswith(".withheld.jsonl") for name in quarantined)
    assert any(name.endswith(".reason.json") for name in quarantined)


def test_zero_record_run_is_withheld(tmp_path):
    dataset = _seeded_dataset(tmp_path)
    before = dataset.read_text()
    staged = tmp_path / "staged.jsonl"
    staged.write_text("", encoding="utf-8")

    stats = publish_dataset(staged, dataset, record_count=0, min_records=25)
    assert stats.published is False
    assert "zero_records" in stats.withheld_reason
    assert dataset.read_text() == before


def test_blocked_run_is_withheld_even_with_enough_records(tmp_path):
    dataset = _seeded_dataset(tmp_path)
    staged = tmp_path / "staged.jsonl"
    append_jsonl(staged, [{"product_title": f"x{i}"} for i in range(60)])

    stats = publish_dataset(staged, dataset, record_count=60, min_records=25, blocked=True)
    assert stats.published is False
    assert stats.withheld_reason == "run_was_blocked"


def test_too_many_suspect_pages_withholds_the_run(tmp_path):
    staged = tmp_path / "staged.jsonl"
    append_jsonl(staged, [{"product_title": f"x{i}"} for i in range(60)])
    stats = publish_dataset(
        staged, tmp_path / "dataset.jsonl", record_count=60, min_records=25,
        suspect_ratio=0.8, max_suspect_ratio=0.34,
    )
    assert stats.published is False
    assert "suspect_page_ratio" in stats.withheld_reason


def test_valid_run_publishes_and_backs_up_the_previous_file(tmp_path):
    dataset = _seeded_dataset(tmp_path)
    before = dataset.read_text()
    staged = tmp_path / "staged.jsonl"
    append_jsonl(staged, [{"product_title": f"new {i}"} for i in range(60)])

    stats = publish_dataset(staged, dataset, record_count=60, min_records=25)

    assert stats.published is True
    assert stats.withheld_reason == ""
    assert len(list(read_jsonl(dataset))) == 60
    assert dataset.with_suffix(".jsonl.prev").read_text() == before
    assert stats.sha256 == sha256_file(dataset)


def test_missing_staged_file_is_reported_not_crashing(tmp_path):
    stats = publish_dataset(
        tmp_path / "nope.jsonl", tmp_path / "dataset.jsonl", record_count=30, min_records=25
    )
    assert stats.published is False
    assert "staged_file_missing" in stats.withheld_reason


def test_stats_serialises_to_json(tmp_path):
    staged = tmp_path / "staged.jsonl"
    append_jsonl(staged, [{"a": 1}] * 30)
    stats = publish_dataset(staged, tmp_path / "dataset.jsonl", record_count=30, min_records=25)
    payload = json.loads(json.dumps(stats.as_dict()))
    assert payload["published"] is True
    assert payload["record_count"] == 30
