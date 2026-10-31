"""Reads Forza Horizon 5's UDP "Data Out" telemetry stream.

Unlike rFactor2 (shared memory + a plugin), Forza's telemetry is just a
UDP broadcast - no plugin, no shared memory, nothing to install. Turn it
on in-game: Settings -> HUD and Gameplay -> Data Out -> On, IP Address =
127.0.0.1 (this PC), IP Port = ForzaUDPReader.DEFAULT_PORT below (or
whatever you pass to ForzaUDPReader(port=...) - the two just have to
match). No Format field to set in Horizon - unlike Forza Motorsport,
which lets you pick "Sled" vs "Dash", Horizon always sends the one
fixed packet shape this file parses.

Packet layout (ForzaDashPacket below) - NOT hand-typed from memory. A
324-byte "Horizon" format Dash packet, cross-checked 2026-08 between two
independent sources: a web search summary describing "324-byte packets"
with "12 extra Horizon-only bytes after NumCylinders" (CarCategory +
2x HorizonUnknown, inserted between what Forza Motorsport's Sled format
already has and where the Dash-only extension - position/speed/lap/input
data - begins), and the community-maintained field-by-field list at
https://github.com/richstokes/Forza-data-tools (FH4_packetformat.dat,
same "Horizon" family format FH5 uses). Both agreed. A wrong byte offset
here would silently corrupt every field after it, so this was verified
against real sources rather than guessed.

Gear byte's exact reverse/neutral/1st-gear numeric encoding could NOT be
confirmed from any documentation found - the same situation this project
hit with rFactor2's mSector field, resolved the same way there: don't
ship a guessed mapping as fact. See the __main__ block below, which
prints the raw Gear/Accel/Brake bytes live so the exact mapping can be
confirmed by driving and watching them against Forza's own HUD (gear
lights up top of the tach; Accel/Brake should track your actual pedal
inputs 0-255) - the same empirical approach documented in this project's
CLAUDE.md for how mSector's mapping was eventually nailed down for real.

Requires: nothing beyond the standard library (ctypes, socket).
"""
import ctypes
import socket
import time


class ForzaDashPacket(ctypes.LittleEndianStructure):
    """Forza Horizon's "Car Dash" Data Out packet, 324 bytes, all fields
    named (not skipped via padding) so the layout stays self-documenting
    and auditable field-by-field against the sources in this module's
    docstring. Only a handful of these are actually read elsewhere in
    this file - the rest exist so the struct's total size is provably
    correct (see the sizeof() assertion below) rather than hoping a
    padding count adds up."""
    _pack_ = 1
    _fields_ = [
        ("IsRaceOn", ctypes.c_int32),
        ("TimestampMS", ctypes.c_uint32),
        ("EngineMaxRpm", ctypes.c_float),
        ("EngineIdleRpm", ctypes.c_float),
        ("CurrentEngineRpm", ctypes.c_float),
        ("AccelerationX", ctypes.c_float),
        ("AccelerationY", ctypes.c_float),
        ("AccelerationZ", ctypes.c_float),
        ("VelocityX", ctypes.c_float),
        ("VelocityY", ctypes.c_float),
        ("VelocityZ", ctypes.c_float),
        ("AngularVelocityX", ctypes.c_float),
        ("AngularVelocityY", ctypes.c_float),
        ("AngularVelocityZ", ctypes.c_float),
        ("Yaw", ctypes.c_float),
        ("Pitch", ctypes.c_float),
        ("Roll", ctypes.c_float),
        ("NormalizedSuspensionTravelFrontLeft", ctypes.c_float),
        ("NormalizedSuspensionTravelFrontRight", ctypes.c_float),
        ("NormalizedSuspensionTravelRearLeft", ctypes.c_float),
        ("NormalizedSuspensionTravelRearRight", ctypes.c_float),
        ("TireSlipRatioFrontLeft", ctypes.c_float),
        ("TireSlipRatioFrontRight", ctypes.c_float),
        ("TireSlipRatioRearLeft", ctypes.c_float),
        ("TireSlipRatioRearRight", ctypes.c_float),
        ("WheelRotationSpeedFrontLeft", ctypes.c_float),
        ("WheelRotationSpeedFrontRight", ctypes.c_float),
        ("WheelRotationSpeedRearLeft", ctypes.c_float),
        ("WheelRotationSpeedRearRight", ctypes.c_float),
        ("WheelOnRumbleStripFrontLeft", ctypes.c_int32),
        ("WheelOnRumbleStripFrontRight", ctypes.c_int32),
        ("WheelOnRumbleStripRearLeft", ctypes.c_int32),
        ("WheelOnRumbleStripRearRight", ctypes.c_int32),
        ("WheelInPuddleDepthFrontLeft", ctypes.c_float),
        ("WheelInPuddleDepthFrontRight", ctypes.c_float),
        ("WheelInPuddleDepthRearLeft", ctypes.c_float),
        ("WheelInPuddleDepthRearRight", ctypes.c_float),
        ("SurfaceRumbleFrontLeft", ctypes.c_float),
        ("SurfaceRumbleFrontRight", ctypes.c_float),
        ("SurfaceRumbleRearLeft", ctypes.c_float),
        ("SurfaceRumbleRearRight", ctypes.c_float),
        ("TireSlipAngleFrontLeft", ctypes.c_float),
        ("TireSlipAngleFrontRight", ctypes.c_float),
        ("TireSlipAngleRearLeft", ctypes.c_float),
        ("TireSlipAngleRearRight", ctypes.c_float),
        ("TireCombinedSlipFrontLeft", ctypes.c_float),
        ("TireCombinedSlipFrontRight", ctypes.c_float),
        ("TireCombinedSlipRearLeft", ctypes.c_float),
        ("TireCombinedSlipRearRight", ctypes.c_float),
        ("SuspensionTravelMetersFrontLeft", ctypes.c_float),
        ("SuspensionTravelMetersFrontRight", ctypes.c_float),
        ("SuspensionTravelMetersRearLeft", ctypes.c_float),
        ("SuspensionTravelMetersRearRight", ctypes.c_float),
        ("CarOrdinal", ctypes.c_int32),
        ("CarClass", ctypes.c_int32),
        ("CarPerformanceIndex", ctypes.c_int32),
        ("DrivetrainType", ctypes.c_int32),
        ("NumCylinders", ctypes.c_int32),
        # --- Horizon-only (not present in Forza Motorsport's Sled/Dash) ---
        ("CarCategory", ctypes.c_int32),
        ("HorizonUnknown1", ctypes.c_uint32),
        ("HorizonUnknown2", ctypes.c_uint32),
        # --- Dash-format extension (position/speed/lap/pedal-input data) ---
        ("PositionX", ctypes.c_float),
        ("PositionY", ctypes.c_float),
        ("PositionZ", ctypes.c_float),
        ("Speed", ctypes.c_float),   # metres/second - convert to km/h for display
        ("Power", ctypes.c_float),
        ("Torque", ctypes.c_float),
        ("TireTempFrontLeft", ctypes.c_float),
        ("TireTempFrontRight", ctypes.c_float),
        ("TireTempRearLeft", ctypes.c_float),
        ("TireTempRearRight", ctypes.c_float),
        ("Boost", ctypes.c_float),
        ("Fuel", ctypes.c_float),
        ("DistanceTraveled", ctypes.c_float),
        ("BestLap", ctypes.c_float),
        ("LastLap", ctypes.c_float),
        ("CurrentLap", ctypes.c_float),
        ("CurrentRaceTime", ctypes.c_float),
        ("LapNumber", ctypes.c_uint16),
        ("RacePosition", ctypes.c_uint8),
        ("Accel", ctypes.c_uint8),          # 0-255
        ("Brake", ctypes.c_uint8),          # 0-255
        ("Clutch", ctypes.c_uint8),         # 0-255
        ("HandBrake", ctypes.c_uint8),      # 0-255
        ("Gear", ctypes.c_uint8),           # exact encoding unconfirmed - see module docstring
        ("Steer", ctypes.c_int8),
        ("NormalizedDrivingLine", ctypes.c_int8),
        ("NormalizedAIBrakeDifference", ctypes.c_int8),
        ("HorizonTrailingUnknown", ctypes.c_uint8),
    ]


assert ctypes.sizeof(ForzaDashPacket) == 324, (
    f"ForzaDashPacket is {ctypes.sizeof(ForzaDashPacket)} bytes, expected 324 - "
    "the field layout above no longer matches its sources, fix before trusting any of it"
)


class PlayerTelemetry:
    __slots__ = ("gear", "rpm", "rpm_max", "speed_kph", "lap_number")

    def __init__(self, gear, rpm, rpm_max, speed_kph, lap_number):
        self.gear = gear
        self.rpm = rpm
        self.rpm_max = rpm_max
        self.speed_kph = speed_kph
        self.lap_number = lap_number


class LapTiming:
    __slots__ = ("lap_time_s", "lap_number")

    def __init__(self, lap_time_s, lap_number):
        self.lap_time_s = lap_time_s
        self.lap_number = lap_number


class ForzaUDPReader:
    """Listens for Forza's Data Out UDP packets. Unlike rFactor2's shared
    memory (which exists independently of whether we're reading it, so a
    failed open specifically means "game not running yet" and is worth
    retrying), a UDP socket binds successfully immediately regardless of
    whether Forza is sending anything - "no packets yet" just means
    get_player_telemetry() returns None / telemetry_age() stays large,
    the same as it would mid-session if Data Out gets turned off. No
    separate "wait for the source to exist" step needed the way rF2's
    reader has."""

    DEFAULT_PORT = 5300

    def __init__(self, port=DEFAULT_PORT, bind_addr="0.0.0.0"):
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setblocking(False)
        self._sock.bind((bind_addr, port))
        self._last_packet = None
        self._last_packet_time = None

    def close(self):
        self._sock.close()

    def poll(self):
        """Drain every pending packet, keeping only the most recent valid
        one - call this once per frame before reading telemetry."""
        while True:
            try:
                data, _addr = self._sock.recvfrom(2048)
            except BlockingIOError:
                break
            if len(data) != ctypes.sizeof(ForzaDashPacket):
                # Wrong Data Out format selected in-game (e.g. "Sled"
                # instead of "Car Dash"), or a stray packet from something
                # else entirely - ignore rather than misparse it.
                continue
            self._last_packet = ForzaDashPacket.from_buffer_copy(data)
            self._last_packet_time = time.time()

    def telemetry_age(self):
        """Seconds since the last valid packet, or infinity if none yet."""
        if self._last_packet_time is None:
            return float("inf")
        return time.time() - self._last_packet_time

    def get_player_telemetry(self):
        p = self._last_packet
        if p is None or not p.IsRaceOn:
            return None
        return PlayerTelemetry(
            gear=p.Gear,
            rpm=p.CurrentEngineRpm,
            rpm_max=p.EngineMaxRpm,
            speed_kph=p.Speed * 3.6,   # Speed is metres/second on the wire
            lap_number=p.LapNumber,
        )

    def get_player_lap_timing(self):
        p = self._last_packet
        if p is None or not p.IsRaceOn:
            return None
        return LapTiming(lap_time_s=p.CurrentLap, lap_number=p.LapNumber)


if __name__ == "__main__":
    # Standalone diagnostic - same idiom as volgas_rf2_telemetry.py's own
    # __main__ block: run this directly (safe alongside Volgas.exe, UDP
    # sockets support multiple listeners) to see live values and, in
    # particular, to nail down Gear's exact numeric encoding by watching
    # it against Forza's own on-screen gear indicator while driving.
    reader = ForzaUDPReader()
    print(f"Listening on UDP port {reader.port}. In Forza: Settings -> HUD and "
          f"Gameplay -> Data Out -> On, IP Address 127.0.0.1, IP Port {reader.port}.")
    print("Waiting for packets... Ctrl+C to stop.\n")
    try:
        while True:
            reader.poll()
            pt = reader.get_player_telemetry()
            p = reader._last_packet
            if pt is not None:
                line = (f"gear={pt.gear} rpm={pt.rpm:7.0f}/{pt.rpm_max:.0f} "
                        f"speed={pt.speed_kph:6.1f}kph lap={pt.lap_number} "
                        f"| raw: accel={p.Accel:3d} brake={p.Brake:3d} clutch={p.Clutch:3d} "
                        f"handbrake={p.HandBrake:3d} isRaceOn={p.IsRaceOn}")
                print(f"\r{line}   ", end="")
            elif p is not None:
                print(f"\rgetting packets but IsRaceOn=0 (paused/menu/replay?)   ", end="")
            else:
                age = reader.telemetry_age()
                print(f"\rno packets yet (age={'inf' if age == float('inf') else f'{age:.1f}s'}) "
                      f"- check Data Out is On and the port/IP match   ", end="")
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()
        print("\nstopped")
