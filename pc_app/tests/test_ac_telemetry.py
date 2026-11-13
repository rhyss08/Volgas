"""Assetto Corsa shared-memory layouts: field offsets from AC's Shared Memory
Reference (4-byte packing, 2-byte wchar_t)."""
import ctypes

import volgas_ac_telemetry as ac


def offset(struct, field):
    return getattr(struct, field).offset


def test_physics_offsets():
    assert offset(ac.ACPhysics, "gear") == 16
    assert offset(ac.ACPhysics, "rpms") == 20
    assert offset(ac.ACPhysics, "speedKmh") == 28
    assert offset(ac.ACPhysics, "numberOfTyresOut") == 244


def test_graphics_offsets():
    assert offset(ac.ACGraphics, "status") == 4
    assert offset(ac.ACGraphics, "completedLaps") == 132
    assert offset(ac.ACGraphics, "iCurrentTime") == 140
    assert offset(ac.ACGraphics, "isInPit") == 160
    assert offset(ac.ACGraphics, "normalizedCarPosition") == 248


def test_static_offsets():
    assert offset(ac.ACStatic, "sectorCount") == 400
    assert offset(ac.ACStatic, "maxRpm") == 412
    assert offset(ac.ACStatic, "trackSPlineLength") == 520
    assert ctypes.sizeof(ac.ACStatic) == 524
