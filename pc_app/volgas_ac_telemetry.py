"""Assetto Corsa telemetry from the game's own shared memory (no plugin or
setting needed - AC publishes it whenever it's running).

Three named mappings, laid out as in AC's "Shared Memory Reference"
(SPageFilePhysics / SPageFileGraphic / SPageFileStatic, 4-byte packing).
Only the leading fields Volgas uses are declared; the game's structs are
longer, which is fine for reading a prefix. Assetto Corsa Competizione uses
the same names and the same layout for these fields.

Strings in AC's structs are wchar_t (2 bytes on Windows); they're declared
as uint16 arrays here so the offsets are the same on any platform (the
tests check them).
"""
import ctypes
import sys
import time

PHYSICS_NAME = "Local\\acpmf_physics"
GRAPHICS_NAME = "Local\\acpmf_graphics"
STATIC_NAME = "Local\\acpmf_static"

AC_STATUS_LIVE = 2
# AC's own rule of thumb (and the server default): a lap with more than two
# tyres off the track at any point doesn't count.
MAX_TYRES_OUT = 2
DEFAULT_TRACK_LENGTH_M = 5000.0  # if the game doesn't report the track length


def _wchars(n):
    return ctypes.c_uint16 * n


class ACPhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32),
        ("gas", ctypes.c_float),
        ("brake", ctypes.c_float),
        ("fuel", ctypes.c_float),
        ("gear", ctypes.c_int32),        # 0 = reverse, 1 = neutral, 2 = first...
        ("rpms", ctypes.c_int32),
        ("steerAngle", ctypes.c_float),
        ("speedKmh", ctypes.c_float),
        ("velocity", ctypes.c_float * 3),
        ("accG", ctypes.c_float * 3),
        ("wheelSlip", ctypes.c_float * 4),
        ("wheelLoad", ctypes.c_float * 4),
        ("wheelsPressure", ctypes.c_float * 4),
        ("wheelAngularSpeed", ctypes.c_float * 4),
        ("tyreWear", ctypes.c_float * 4),
        ("tyreDirtyLevel", ctypes.c_float * 4),
        ("tyreCoreTemperature", ctypes.c_float * 4),
        ("camberRAD", ctypes.c_float * 4),
        ("suspensionTravel", ctypes.c_float * 4),
        ("drs", ctypes.c_float),
        ("tc", ctypes.c_float),
        ("heading", ctypes.c_float),
        ("pitch", ctypes.c_float),
        ("roll", ctypes.c_float),
        ("cgHeight", ctypes.c_float),
        ("carDamage", ctypes.c_float * 5),
        ("numberOfTyresOut", ctypes.c_int32),
        ("pitLimiterOn", ctypes.c_int32),
    ]


class ACGraphics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32),
        ("status", ctypes.c_int32),      # 0 off, 1 replay, 2 live, 3 pause
        ("session", ctypes.c_int32),
        ("currentTime", _wchars(15)),
        ("lastTime", _wchars(15)),
        ("bestTime", _wchars(15)),
        ("split", _wchars(15)),
        ("completedLaps", ctypes.c_int32),
        ("position", ctypes.c_int32),
        ("iCurrentTime", ctypes.c_int32),  # ms
        ("iLastTime", ctypes.c_int32),
        ("iBestTime", ctypes.c_int32),
        ("sessionTimeLeft", ctypes.c_float),
        ("distanceTraveled", ctypes.c_float),
        ("isInPit", ctypes.c_int32),
        ("currentSectorIndex", ctypes.c_int32),
        ("lastSectorTime", ctypes.c_int32),
        ("numberOfLaps", ctypes.c_int32),
        ("tyreCompound", _wchars(33)),
        ("replayTimeMultiplier", ctypes.c_float),
        ("normalizedCarPosition", ctypes.c_float),  # 0..1 around the lap
    ]


class ACStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", _wchars(15)),
        ("acVersion", _wchars(15)),
        ("numberOfSessions", ctypes.c_int32),
        ("numCars", ctypes.c_int32),
        ("carModel", _wchars(33)),
        ("track", _wchars(33)),
        ("playerName", _wchars(33)),
        ("playerSurname", _wchars(33)),
        ("playerNick", _wchars(33)),
        ("sectorCount", ctypes.c_int32),
        ("maxTorque", ctypes.c_float),
        ("maxPower", ctypes.c_float),
        ("maxRpm", ctypes.c_int32),
        ("maxFuel", ctypes.c_float),
        ("suspensionMaxTravel", ctypes.c_float * 4),
        ("tyreRadius", ctypes.c_float * 4),
        ("maxTurboBoost", ctypes.c_float),
        ("airTemp", ctypes.c_float),
        ("roadTemp", ctypes.c_float),
        ("penaltiesEnabled", ctypes.c_int32),
        ("aidFuelRate", ctypes.c_float),
        ("aidTireRate", ctypes.c_float),
        ("aidMechanicalDamage", ctypes.c_float),
        ("aidAllowTyreBlankets", ctypes.c_int32),
        ("aidStability", ctypes.c_float),
        ("aidAutoClutch", ctypes.c_int32),
        ("aidAutoBlip", ctypes.c_int32),
        ("hasDRS", ctypes.c_int32),
        ("hasERS", ctypes.c_int32),
        ("hasKERS", ctypes.c_int32),
        ("kersMaxJ", ctypes.c_float),
        ("engineBrakeSettingsCount", ctypes.c_int32),
        ("ersPowerControllerCount", ctypes.c_int32),
        ("trackSPlineLength", ctypes.c_float),  # metres
    ]


class ACTelemetry:
    __slots__ = ("gear", "rpm", "rpm_max", "speed_kph")

    def __init__(self, gear, rpm, rpm_max, speed_kph):
        self.gear, self.rpm, self.rpm_max, self.speed_kph = gear, rpm, rpm_max, speed_kph


class ACLapTiming:
    """lap_dist is metres into the lap, or -1 in the pits (like rFactor 2's
    mLapDist), so volgas_rf2_dash.LapRecorder can build the live delta;
    fraction is the same position as 0..1."""
    __slots__ = ("lap_time_s", "lap_dist", "fraction", "completed_laps", "tyres_out")

    def __init__(self, lap_time_s, lap_dist, fraction, completed_laps, tyres_out):
        self.lap_time_s, self.lap_dist, self.fraction = lap_time_s, lap_dist, fraction
        self.completed_laps, self.tyres_out = completed_laps, tyres_out


if sys.platform == "win32":
    from ctypes import wintypes

    FILE_MAP_READ = 0x0004
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.OpenFileMappingW.restype = wintypes.HANDLE
    _kernel32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.c_size_t]
    _kernel32.MapViewOfFile.restype = ctypes.c_void_p
    _kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    _kernel32.UnmapViewOfFile.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


class _Mapping:
    def __init__(self, name, struct_type):
        if sys.platform != "win32":
            raise OSError("Assetto Corsa shared memory is Windows-only")
        self.struct_type = struct_type
        self._addr = None
        self._handle = _kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
        if not self._handle:
            raise OSError(f"Assetto Corsa isn't running ({name} not found)")
        self._addr = _kernel32.MapViewOfFile(self._handle, FILE_MAP_READ, 0, 0, ctypes.sizeof(struct_type))
        if not self._addr:
            _kernel32.CloseHandle(self._handle)
            self._handle = None
            raise OSError(f"could not map {name} (err={ctypes.get_last_error()})")

    def read(self):
        return self.struct_type.from_buffer_copy(
            (ctypes.c_ubyte * ctypes.sizeof(self.struct_type)).from_address(self._addr))

    def close(self):
        if self._addr:
            _kernel32.UnmapViewOfFile(self._addr)
            self._addr = None
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None


class ACSharedMemoryReader:
    """Raises OSError while Assetto Corsa isn't running. The game keeps
    the mappings' last values after it stops updating them (pause, menus,
    or exit while we hold a handle), so freshness is judged by physics
    packetId changing - see telemetry_age()."""

    def __init__(self):
        self._maps = []
        try:
            self._physics = self._open(PHYSICS_NAME, ACPhysics)
            self._graphics = self._open(GRAPHICS_NAME, ACGraphics)
            self._static = self._open(STATIC_NAME, ACStatic)
        except OSError:
            self.close()
            raise
        self._p = self._g = self._s = None
        self._last_packet = None
        self._last_update = 0.0

    def _open(self, name, struct_type):
        m = _Mapping(name, struct_type)
        self._maps.append(m)
        return m

    def poll(self):
        p = self._physics.read()
        g = self._graphics.read()
        if p.packetId != self._last_packet and g.status == AC_STATUS_LIVE:
            self._last_packet = p.packetId
            self._last_update = time.time()
            self._p, self._g, self._s = p, g, self._static.read()

    def telemetry_age(self):
        return float("inf") if self._last_update == 0.0 else time.time() - self._last_update

    def get_player_telemetry(self):
        if self._p is None:
            return None
        return ACTelemetry(gear=self._p.gear - 1, rpm=self._p.rpms, rpm_max=self._s.maxRpm,
                           speed_kph=self._p.speedKmh)

    def get_lap_timing(self):
        if self._g is None:
            return None
        length = self._s.trackSPlineLength if self._s.trackSPlineLength > 0 else DEFAULT_TRACK_LENGTH_M
        fraction = self._g.normalizedCarPosition
        return ACLapTiming(lap_time_s=self._g.iCurrentTime / 1000.0,
                           lap_dist=-1.0 if self._g.isInPit else fraction * length, fraction=fraction,
                           completed_laps=self._g.completedLaps, tyres_out=self._p.numberOfTyresOut)

    def close(self):
        for m in self._maps:
            m.close()
        self._maps = []
