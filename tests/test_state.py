"""Resume-after-interruption checkpoints."""

from __future__ import annotations

from walmart_scraper.state import RunState, listing_key, product_key


def test_keys_are_namespaced():
    assert listing_key("2457", "https://w/x", 2) == "listing::2457::https://w/x::2"
    assert product_key("2457", "123") == "product::2457::123"


def test_save_and_reload(tmp_path):
    path = tmp_path / "state.json"
    state = RunState.load(path, resume=False, run_id="run1")
    state.mark_listing_done(listing_key("2457", "u", 1))
    state.mark_product_done(product_key("2457", "123"))
    state.note_item("78745:123")
    state.note_blocked("https://w/blocked")
    state.add_records(7)
    state.save()

    reloaded = RunState.load(path, resume=True)
    assert reloaded.run_id == "run1"
    assert reloaded.is_listing_done(listing_key("2457", "u", 1))
    assert reloaded.is_product_done(product_key("2457", "123"))
    assert reloaded.already_seen("78745:123")
    assert reloaded.blocked_urls == {"https://w/blocked"}
    assert reloaded.records_staged == 7
    assert reloaded.checkpoints == 1


def test_a_fresh_run_ignores_the_checkpoint(tmp_path):
    path = tmp_path / "state.json"
    state = RunState.load(path, resume=False)
    state.mark_listing_done(listing_key("2457", "u", 1))
    state.add_records(3)
    state.save()

    fresh = RunState.load(path, resume=False)
    assert fresh.is_listing_done(listing_key("2457", "u", 1)) is False
    assert fresh.records_staged == 0


def test_corrupt_checkpoint_does_not_abort_the_run(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    state = RunState.load(path, resume=True)
    assert state.records_staged == 0
    assert state.completed_listings == set()


def test_missing_checkpoint_file_is_fine(tmp_path):
    state = RunState.load(tmp_path / "absent.json", resume=True)
    assert state.resume is True
    assert state.completed_listings == set()


def test_save_is_repeatable_and_counts_checkpoints(tmp_path):
    state = RunState.load(tmp_path / "state.json")
    state.save()
    state.save()
    assert RunState.load(tmp_path / "state.json", resume=True).checkpoints == 2


def test_clear_removes_state(tmp_path):
    path = tmp_path / "state.json"
    state = RunState.load(path)
    state.mark_listing_done("listing::x")
    state.save()
    state.clear()
    assert not path.exists()
    assert state.completed_listings == set()


def test_add_records_ignores_negative_values(tmp_path):
    state = RunState.load(tmp_path / "state.json")
    state.add_records(5)
    state.add_records(-3)
    assert state.records_staged == 5
