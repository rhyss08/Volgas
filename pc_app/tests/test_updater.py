"""Self-updater: a failed swap must never leave the user without Volgas.exe."""
import os
import subprocess

import pytest

import volgas


def test_failed_swap_restores_previous_exe(tmp_path, monkeypatch):
    exe = tmp_path / "Volgas.exe"
    exe.write_bytes(b"old version")
    monkeypatch.setattr(volgas.sys, "executable", str(exe))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not start anything"))
    missing_download = tmp_path / "Volgas.exe.new"  # doesn't exist, so the second rename fails
    with pytest.raises(OSError):
        volgas._install_update(str(missing_download))
    assert exe.read_bytes() == b"old version"
    assert not (tmp_path / "Volgas.exe.old").exists()


def test_successful_swap(tmp_path, monkeypatch):
    exe = tmp_path / "Volgas.exe"
    exe.write_bytes(b"old version")
    new = tmp_path / "Volgas.exe.new"
    new.write_bytes(b"new version")
    started = []
    monkeypatch.setattr(volgas.sys, "executable", str(exe))
    monkeypatch.setattr(volgas.subprocess, "Popen", lambda args, **k: started.append(args))
    volgas._install_update(str(new))
    assert exe.read_bytes() == b"new version"
    assert (tmp_path / "Volgas.exe.old").read_bytes() == b"old version"
    assert started == [[str(exe)]]


def test_version_compare():
    assert volgas._version_tuple("v0.2.10") > volgas._version_tuple("0.2.4")
