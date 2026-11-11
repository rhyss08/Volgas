"""Settings: atomic saves, damaged files and migration from the old location."""
import json
import os

import volgas_settings as vs


def test_save_then_load(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLGAS_DATA_DIR", str(tmp_path))
    assert vs.save_settings({"check_updates_on_start": False, "wheelbase": {"MotionRange": 540}})
    assert vs.load_settings() == {"check_updates_on_start": False, "wheelbase": {"MotionRange": 540}}
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]  # no temp file left behind


def test_damaged_file_is_set_aside(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLGAS_DATA_DIR", str(tmp_path))
    (tmp_path / vs.SETTINGS_FILE_NAME).write_text('{"half written": ')
    assert vs.load_settings() == {}
    assert any(p.startswith(vs.SETTINGS_FILE_NAME + ".corrupt-") for p in os.listdir(tmp_path))


def test_non_object_file_is_set_aside(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLGAS_DATA_DIR", str(tmp_path))
    (tmp_path / vs.SETTINGS_FILE_NAME).write_text("[1, 2, 3]")
    assert vs.load_settings() == {}


def test_migrates_from_old_location_and_keeps_old_file(tmp_path, monkeypatch):
    data_dir, old_dir = tmp_path / "data", tmp_path / "exe"
    old_dir.mkdir()
    old_file = old_dir / vs.SETTINGS_FILE_NAME
    old_file.write_text(json.dumps({"wheelbase": {"PowerLimit": 30}}))
    monkeypatch.setenv("VOLGAS_DATA_DIR", str(data_dir))
    monkeypatch.setattr(vs, "legacy_settings_path", lambda: str(old_file))
    assert vs.load_settings() == {"wheelbase": {"PowerLimit": 30}}
    assert json.loads((data_dir / vs.SETTINGS_FILE_NAME).read_text()) == {"wheelbase": {"PowerLimit": 30}}
    assert old_file.exists()  # an older Volgas can still read it after a downgrade


def test_save_failure_returns_false(tmp_path, monkeypatch):
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("x")
    monkeypatch.setenv("VOLGAS_DATA_DIR", str(blocker / "sub"))
    assert vs.save_settings({"a": 1}) is False
