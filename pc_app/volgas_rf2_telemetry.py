"""rFactor2 shared-memory telemetry reader.

Byte-accurate ctypes port of the structs in TheIronWolfModding's
rF2SharedMemoryMapPlugin (docs/rf2_reference/rF2State.h, MappedBuffer.h),
fetched from the plugin's actual GitHub source rather than guessed -
field order/types/the struct's 4-byte packing all come from there.

Reads two Windows named shared-memory blocks the plugin exposes while
rFactor2 is running with it installed in Bin64/Plugins:
  $rFactor2SMMP_Telemetry$ - per-vehicle physics/engine data, ~50Hz
  $rFactor2SMMP_Scoring$   - per-vehicle race state (laps, position), ~5Hz

Each block is [rF2MappedBufferVersionBlock][payload struct]. The plugin
increments mVersionUpdateBegin before writing and mVersionUpdateEnd after;
a reader must check both matched before AND after copying the payload,
or it can read a torn (half-written) frame - see MappedBuffer.h's
ReadUpdate()/VerifyBusyOrUnchanged(), reproduced here as _read_update().

Not yet run against a live rFactor2 session (rFactor2 wasn't running
during development) - the struct layout is transcribed carefully from
the authoritative header, but treat first real use as the actual test.

No extra dependencies - the named shared memory is opened via raw
ctypes calls into kernel32 (OpenFileMappingW/MapViewOfFile), not pywin32.
"""

import ctypes
import math
import time
from ctypes import wintypes

# ==========================================
#   ctypes structs (see docs/rf2_reference/rF2State.h - #pragma pack(push, 4))
# ==========================================

PACK = 4


class RF2Vec3(ctypes.Structure):
    _pack_ = PACK
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("z", ctypes.c_double)]


class RF2Wheel(ctypes.Structure):
    _pack_ = PACK
    _fields_ = [
        ("mSuspensionDeflection", ctypes.c_double),
        ("mRideHeight", ctypes.c_double),
        ("mSuspForce", ctypes.c_double),
        ("mBrakeTemp", ctypes.c_double),
        ("mBrakePressure", ctypes.c_double),
        ("mRotation", ctypes.c_double),
        ("mLateralPatchVel", ctypes.c_double),
        ("mLongitudinalPatchVel", ctypes.c_double),
        ("mLateralGroundVel", ctypes.c_double),
        ("mLongitudinalGroundVel", ctypes.c_double),
        ("mCamber", ctypes.c_double),
        ("mLateralForce", ctypes.c_double),
        ("mLongitudinalForce", ctypes.c_double),
        ("mTireLoad", ctypes.c_double),
        ("mGripFract", ctypes.c_double),
        ("mPressure", ctypes.c_double),
        ("mTemperature", ctypes.c_double * 3),
        ("mWear", ctypes.c_double),
        ("mTerrainName", ctypes.c_char * 16),
        ("mSurfaceType", ctypes.c_ubyte),
        ("mFlat", ctypes.c_bool),
        ("mDetached", ctypes.c_bool),
        ("mStaticUndeflectedRadius", ctypes.c_ubyte),
        ("mVerticalTireDeflection", ctypes.c_double),
        ("mWheelYLocation", ctypes.c_double),
        ("mToe", ctypes.c_double),
        ("mTireCarcassTemperature", ctypes.c_double),
        ("mTireInnerLayerTemperature", ctypes.c_double * 3),
        ("mExpansion", ctypes.c_ubyte * 24),
    ]


class RF2VehicleTelemetry(ctypes.Structure):
    _pack_ = PACK
    _fields_ = [
        ("mID", ctypes.c_long),
        ("mDeltaTime", ctypes.c_double),
        ("mElapsedTime", ctypes.c_double),
        ("mLapNumber", ctypes.c_long),
        ("mLapStartET", ctypes.c_double),
        ("mVehicleName", ctypes.c_char * 64),
        ("mTrackName", ctypes.c_char * 64),
        ("mPos", RF2Vec3),
        ("mLocalVel", RF2Vec3),
        ("mLocalAccel", RF2Vec3),
        ("mOri", RF2Vec3 * 3),
        ("mLocalRot", RF2Vec3),
        ("mLocalRotAccel", RF2Vec3),
        ("mGear", ctypes.c_long),
        ("mEngineRPM", ctypes.c_double),
        ("mEngineWaterTemp", ctypes.c_double),
        ("mEngineOilTemp", ctypes.c_double),
        ("mClutchRPM", ctypes.c_double),
        ("mUnfilteredThrottle", ctypes.c_double),
        ("mUnfilteredBrake", ctypes.c_double),
        ("mUnfilteredSteering", ctypes.c_double),
        ("mUnfilteredClutch", ctypes.c_double),
        ("mFilteredThrottle", ctypes.c_double),
        ("mFilteredBrake", ctypes.c_double),
        ("mFilteredSteering", ctypes.c_double),
        ("mFilteredClutch", ctypes.c_double),
        ("mSteeringShaftTorque", ctypes.c_double),
        ("mFront3rdDeflection", ctypes.c_double),
        ("mRear3rdDeflection", ctypes.c_double),
        ("mFrontWingHeight", ctypes.c_double),
        ("mFrontRideHeight", ctypes.c_double),
        ("mRearRideHeight", ctypes.c_double),
        ("mDrag", ctypes.c_double),
        ("mFrontDownforce", ctypes.c_double),
        ("mRearDownforce", ctypes.c_double),
        ("mFuel", ctypes.c_double),
        ("mEngineMaxRPM", ctypes.c_double),
        ("mScheduledStops", ctypes.c_ubyte),
        ("mOverheating", ctypes.c_bool),
        ("mDetached", ctypes.c_bool),
        ("mHeadlights", ctypes.c_bool),
        ("mDentSeverity", ctypes.c_ubyte * 8),
        ("mLastImpactET", ctypes.c_double),
        ("mLastImpactMagnitude", ctypes.c_double),
        ("mLastImpactPos", RF2Vec3),
        ("mEngineTorque", ctypes.c_double),
        ("mCurrentSector", ctypes.c_long),
        ("mSpeedLimiter", ctypes.c_ubyte),
        ("mMaxGears", ctypes.c_ubyte),
        ("mFrontTireCompoundIndex", ctypes.c_ubyte),
        ("mRearTireCompoundIndex", ctypes.c_ubyte),
        ("mFuelCapacity", ctypes.c_double),
        ("mFrontFlapActivated", ctypes.c_ubyte),
        ("mRearFlapActivated", ctypes.c_ubyte),
        ("mRearFlapLegalStatus", ctypes.c_ubyte),
        ("mIgnitionStarter", ctypes.c_ubyte),
        ("mFrontTireCompoundName", ctypes.c_char * 18),
        ("mRearTireCompoundName", ctypes.c_char * 18),
        ("mSpeedLimiterAvailable", ctypes.c_ubyte),
        ("mAntiStallActivated", ctypes.c_ubyte),
        ("mUnused", ctypes.c_ubyte * 2),
        ("mVisualSteeringWheelRange", ctypes.c_float),
        ("mRearBrakeBias", ctypes.c_double),
        ("mTurboBoostPressure", ctypes.c_double),
        ("mPhysicsToGraphicsOffset", ctypes.c_float * 3),
        ("mPhysicalSteeringWheelRange", ctypes.c_float),
        ("mBatteryChargeFraction", ctypes.c_double),
        ("mElectricBoostMotorTorque", ctypes.c_double),
        ("mElectricBoostMotorRPM", ctypes.c_double),
        ("mElectricBoostMotorTemperature", ctypes.c_double),
        ("mElectricBoostWaterTemperature", ctypes.c_double),
        ("mElectricBoostMotorState", ctypes.c_ubyte),
        ("mExpansion", ctypes.c_ubyte * 111),
        ("mWheels", RF2Wheel * 4),
    ]


MAX_MAPPED_VEHICLES = 128


class RF2Telemetry(ctypes.Structure):
    _pack_ = PACK
    _fields_ = [
        ("mBytesUpdatedHint", ctypes.c_int),
        ("mNumVehicles", ctypes.c_long),
        ("mVehicles", RF2VehicleTelemetry * MAX_MAPPED_VEHICLES),
    ]


class RF2VehicleScoring(ctypes.Structure):
    _pack_ = PACK
    _fields_ = [
        ("mID", ctypes.c_long),
        ("mDriverName", ctypes.c_char * 32),
        ("mVehicleName", ctypes.c_char * 64),
        ("mTotalLaps", ctypes.c_short),
        ("mSector", ctypes.c_byte),
        ("mFinishStatus", ctypes.c_byte),
        ("mLapDist", ctypes.c_double),
        ("mPathLateral", ctypes.c_double),
        ("mTrackEdge", ctypes.c_double),
        ("mBestSector1", ctypes.c_double),
        ("mBestSector2", ctypes.c_double),
        ("mBestLapTime", ctypes.c_double),
        ("mLastSector1", ctypes.c_double),
        ("mLastSector2", ctypes.c_double),
        ("mLastLapTime", ctypes.c_double),
        ("mCurSector1", ctypes.c_double),
        ("mCurSector2", ctypes.c_double),
        ("mNumPitstops", ctypes.c_short),
        ("mNumPenalties", ctypes.c_short),
        ("mIsPlayer", ctypes.c_bool),
        ("mControl", ctypes.c_byte),
        ("mInPits", ctypes.c_bool),
        ("mPlace", ctypes.c_ubyte),
        ("mVehicleClass", ctypes.c_char * 32),
        ("mTimeBehindNext", ctypes.c_double),
        ("mLapsBehindNext", ctypes.c_long),
        ("mTimeBehindLeader", ctypes.c_double),
        ("mLapsBehindLeader", ctypes.c_long),
        ("mLapStartET", ctypes.c_double),
        ("mPos", RF2Vec3),
        ("mLocalVel", RF2Vec3),
        ("mLocalAccel", RF2Vec3),
        ("mOri", RF2Vec3 * 3),
        ("mLocalRot", RF2Vec3),
        ("mLocalRotAccel", RF2Vec3),
        ("mHeadlights", ctypes.c_ubyte),
        ("mPitState", ctypes.c_ubyte),
        ("mServerScored", ctypes.c_ubyte),
        ("mIndividualPhase", ctypes.c_ubyte),
        ("mQualification", ctypes.c_long),
        ("mTimeIntoLap", ctypes.c_double),
        ("mEstimatedLapTime", ctypes.c_double),
        ("mPitGroup", ctypes.c_char * 24),
        ("mFlag", ctypes.c_ubyte),
        ("mUnderYellow", ctypes.c_bool),
        ("mCountLapFlag", ctypes.c_ubyte),
        ("mInGarageStall", ctypes.c_bool),
        ("mUpgradePack", ctypes.c_ubyte * 16),
        ("mPitLapDist", ctypes.c_float),
        ("mBestLapSector1", ctypes.c_float),
        ("mBestLapSector2", ctypes.c_float),
        # Trailing fields beyond this point aren't needed for gear/speed/RPM
        # and weren't transcribed - mExpansion below absorbs the rest of the
        # struct so sizeof() still comes out right for the array stride.
        ("mExpansion", ctypes.c_ubyte * 32),
    ]


class RF2ScoringInfo(ctypes.Structure):
    _pack_ = PACK
    _fields_ = [
        ("mTrackName", ctypes.c_char * 64),
        ("mSession", ctypes.c_long),
        ("mCurrentET", ctypes.c_double),
        ("mEndET", ctypes.c_double),
        ("mMaxLaps", ctypes.c_long),
        ("mLapDist", ctypes.c_double),
        ("pointer1", ctypes.c_ubyte * 8),
        ("mNumVehicles", ctypes.c_long),
        ("mGamePhase", ctypes.c_ubyte),
        ("mYellowFlagState", ctypes.c_byte),
        ("mSectorFlag", ctypes.c_byte * 3),
        ("mStartLight", ctypes.c_ubyte),
        ("mNumRedLights", ctypes.c_ubyte),
        ("mInRealtime", ctypes.c_bool),
        ("mPlayerName", ctypes.c_char * 32),
        ("mPlrFileName", ctypes.c_char * 64),
        ("mDarkCloud", ctypes.c_double),
        ("mRaining", ctypes.c_double),
        ("mAmbientTemp", ctypes.c_double),
        ("mTrackTemp", ctypes.c_double),
        ("mWind", RF2Vec3),
        ("mMinPathWetness", ctypes.c_double),
        ("mMaxPathWetness", ctypes.c_double),
        ("mGameMode", ctypes.c_ubyte),
        ("mIsPasswordProtected", ctypes.c_bool),
        ("mServerPort", ctypes.c_ushort),
        ("mServerPublicIP", ctypes.c_ulong),
        ("mMaxPlayers", ctypes.c_long),
        ("mServerName", ctypes.c_char * 32),
        ("mStartET", ctypes.c_float),
        ("mAvgPathWetness", ctypes.c_double),
        ("mExpansion", ctypes.c_ubyte * 200),
        ("pointer2", ctypes.c_ubyte * 8),
    ]


class RF2Scoring(ctypes.Structure):
    _pack_ = PACK
    _fields_ = [
        ("mBytesUpdatedHint", ctypes.c_int),
        ("mScoringInfo", RF2ScoringInfo),
        ("mVehicles", RF2VehicleScoring * MAX_MAPPED_VEHICLES),
    ]


class RF2MappedBufferVersionBlock(ctypes.Structure):
    _pack_ = PACK
    _fields_ = [("mVersionUpdateBegin", ctypes.c_ulong), ("mVersionUpdateEnd", ctypes.c_ulong)]


TELEMETRY_MM_NAME = "$rFactor2SMMP_Telemetry$"
SCORING_MM_NAME = "$rFactor2SMMP_Scoring$"

# --- Win32 named file mapping (not plain mmap.mmap: the plugin creates the
# mapping itself via CreateFileMappingA: our side must OPEN, not create, or
# risk a permissions/size mismatch) ---
FILE_MAP_READ = 0x0004
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# argtypes must be set explicitly for every call below - without them,
# ctypes assumes plain 32-bit ints for arguments, and a 64-bit pointer
# (like the address MapViewOfFile returns) silently overflows that guess
# instead of erroring until something tries to pass it back in, which is
# exactly what broke UnmapViewOfFile on close().
kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenFileMappingW.restype = wintypes.HANDLE

kernel32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
kernel32.MapViewOfFile.restype = ctypes.c_void_p

kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
kernel32.UnmapViewOfFile.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class SharedMemoryBlock:
    """Opens an existing named file mapping and exposes torn-read-safe reads,
    mirroring MappedBuffer.h's BeginUpdate/EndUpdate/ReadUpdate protocol."""

    def __init__(self, name, payload_type):
        self._addr = None
        self._handle = None
        self.payload_type = payload_type
        self.total_size = ctypes.sizeof(RF2MappedBufferVersionBlock) + ctypes.sizeof(payload_type)
        self._last_version_begin = None
        self._handle = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
        if not self._handle:
            raise OSError(
                f"Could not open shared memory '{name}' - is rFactor2 running with "
                f"rF2SharedMemoryMapPlugin64.dll loaded? (err={ctypes.get_last_error()})"
            )
        addr = kernel32.MapViewOfFile(self._handle, FILE_MAP_READ, 0, 0, self.total_size)
        if not addr:
            kernel32.CloseHandle(self._handle)
            raise OSError(f"Could not map view of '{name}' (err={ctypes.get_last_error()})")
        self._addr = addr
        self._version = RF2MappedBufferVersionBlock.from_address(addr)
        self._payload_addr = addr + ctypes.sizeof(RF2MappedBufferVersionBlock)

    def read(self):
        """Returns a copy of the payload struct, or None if busy/unchanged/torn."""
        begin_before = self._version.mVersionUpdateBegin
        end_before = self._version.mVersionUpdateEnd
        if begin_before != end_before:
            return None  # write in progress
        if self._last_version_begin == begin_before:
            return None  # no new data since last read

        payload = self.payload_type.from_buffer_copy(
            (ctypes.c_ubyte * ctypes.sizeof(self.payload_type)).from_address(self._payload_addr)
        )

        end_after = self._version.mVersionUpdateEnd
        if begin_before != end_after:
            return None  # torn - a write happened while we were copying

        self._last_version_begin = begin_before
        return payload

    def close(self):
        if self._addr:
            kernel32.UnmapViewOfFile(self._addr)
            self._addr = None
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def __del__(self):
        self.close()


class PlayerTelemetry:
    __slots__ = ("gear", "rpm", "rpm_max", "speed_kph", "lap_number", "fuel_l", "fuel_capacity_l")

    def __init__(self, gear, rpm, rpm_max, speed_kph, lap_number, fuel_l, fuel_capacity_l):
        self.gear = gear
        self.rpm = rpm
        self.rpm_max = rpm_max
        self.speed_kph = speed_kph
        self.lap_number = lap_number
        self.fuel_l = fuel_l
        self.fuel_capacity_l = fuel_capacity_l


class PlayerLapTiming:
    """Raw lap/sector timing fields for the player's vehicle, straight from
    RF2VehicleScoring/RF2ScoringInfo - deltas/formatting happen in the
    caller (volgas_rf2_dash.py), not here, so this stays a thin passthrough.

    rF2's convention for a sector/lap time that hasn't happened yet this
    session (e.g. best_sector1 before you've ever finished sector 1) is -1.0 -
    callers must check for that before using cur_/best_ fields in a
    subtraction, or a bogus negative sentinel silently produces a nonsense delta.
    """
    __slots__ = ("lap_time_s", "sector", "cur_sector1", "cur_sector2",
                 "best_sector1", "best_sector2", "best_lap_time", "last_lap_time",
                 "count_lap_flag", "lap_dist", "total_laps")

    def __init__(self, lap_time_s, sector, cur_sector1, cur_sector2,
                 best_sector1, best_sector2, best_lap_time, last_lap_time,
                 count_lap_flag, lap_dist, total_laps):
        self.lap_time_s = lap_time_s
        self.sector = sector
        self.cur_sector1 = cur_sector1
        self.cur_sector2 = cur_sector2
        self.best_sector1 = best_sector1
        self.best_sector2 = best_sector2
        self.best_lap_time = best_lap_time
        self.last_lap_time = last_lap_time
        # Recalled ISI/Studio397 plugin convention (not independently
        # re-verified): 0 = don't count lap or time, 1 = count lap but not
        # time, 2 = count lap and time. == 2 is "valid" for display purposes.
        self.count_lap_flag = count_lap_flag
        self.lap_dist = lap_dist       # mLapDist - "current distance around track" (meters); can be erratic/negative in the pit lane per rF2State.h
        self.total_laps = total_laps   # mTotalLaps - "laps completed"; used to detect lap boundaries for the delta-to-best interpolation


class RF2SharedMemoryReader:
    """High-level reader: tracks the two blocks, resolves which vehicle is
    the player (scoring's mIsPlayer, cross-referenced to telemetry by mID),
    and exposes just the fields the Nano's Classic screen needs."""

    def __init__(self):
        self._telemetry_block = SharedMemoryBlock(TELEMETRY_MM_NAME, RF2Telemetry)
        self._scoring_block = SharedMemoryBlock(SCORING_MM_NAME, RF2Scoring)
        self._last_telemetry = None
        self._last_scoring = None
        # Windows keeps the mapping (and its last-written bytes) valid in our
        # process even after rFactor2 exits, since we still hold our own
        # handle - read() then just keeps returning None ("unchanged") forever
        # rather than erroring, so get_player_telemetry() alone can't tell
        # "game closed" from "car sitting still". Track wall-clock recency of
        # the last actual new frame instead - see telemetry_age().
        self._last_telemetry_update = 0.0

    def poll(self):
        """Call frequently; updates internal state from whatever's new."""
        t = self._telemetry_block.read()
        if t is not None:
            self._last_telemetry = t
            self._last_telemetry_update = time.time()
        s = self._scoring_block.read()
        if s is not None:
            self._last_scoring = s

    def telemetry_age(self):
        """Seconds since the last fresh telemetry frame. Grows unboundedly
        once the game exits (or the plugin stops writing) - callers should
        treat get_player_telemetry()'s result as stale past some threshold."""
        if self._last_telemetry_update == 0.0:
            return float("inf")
        return time.time() - self._last_telemetry_update

    def get_player_telemetry(self):
        """Returns a PlayerTelemetry, or None if no player vehicle found yet
        (e.g. still at the main menu / not yet in a session)."""
        if self._last_telemetry is None or self._last_scoring is None:
            return None

        player_id = None
        for i in range(self._last_scoring.mScoringInfo.mNumVehicles):
            veh = self._last_scoring.mVehicles[i]
            if veh.mIsPlayer:
                player_id = veh.mID
                break
        if player_id is None:
            return None

        for i in range(self._last_telemetry.mNumVehicles):
            veh = self._last_telemetry.mVehicles[i]
            if veh.mID == player_id:
                v = veh.mLocalVel
                speed_ms = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
                return PlayerTelemetry(
                    gear=veh.mGear,
                    rpm=veh.mEngineRPM,
                    rpm_max=veh.mEngineMaxRPM,
                    speed_kph=speed_ms * 3.6,
                    lap_number=veh.mLapNumber,
                    fuel_l=veh.mFuel,
                    fuel_capacity_l=veh.mFuelCapacity,
                )
        return None

    def get_player_lap_timing(self):
        """Returns a PlayerLapTiming for the player's vehicle, or None if no
        scoring data or no player vehicle found yet. mSector's mapping
        (confirmed 2026-07-25 from docs/rf2_reference/rF2State.h line 428,
        live-confirmed by the user too): 0=sector3, 1=sector1, 2=sector2 -
        NOT the sequential 0/1/2->1/2/3 originally assumed. No longer used
        for the delta calculation itself (see volgas_rf2_dash.py's
        LapRecorder, which interpolates continuously against lap_dist
        instead of updating only at sector crossings) - sector/cur_/best_
        sector fields are kept here for any future use, e.g. a sector-split
        readout if that's ever wanted again."""
        if self._last_scoring is None:
            return None

        scoring_info = self._last_scoring.mScoringInfo
        for i in range(scoring_info.mNumVehicles):
            veh = self._last_scoring.mVehicles[i]
            if veh.mIsPlayer:
                # Lap time source, third attempt (2026-07-25) and the first one
                # grounded in the reference header rather than a guess: the
                # TELEMETRY block's own mElapsedTime - mLapStartET, at ~50Hz.
                # The previous two attempts both used the SCORING block:
                #   - scoring's mCurrentET - mLapStartET: only updates at
                #     scoring's ~5Hz, and didn't match the HUD in testing
                #   - mTimeIntoLap: per rF2State.h's own comment this is
                #     "ESTIMATED time into lap" derived from mEstimatedLapTime
                #     - i.e. computed from DISTANCE around the lap, not a
                #     stopwatch, exactly matching the user's "that shows
                #     distance" observation against live testing
                # Falls back to the scoring subtraction if the player's
                # telemetry entry isn't resolvable (e.g. scoring arrived first).
                lap_time_s = scoring_info.mCurrentET - veh.mLapStartET
                if self._last_telemetry is not None:
                    for j in range(self._last_telemetry.mNumVehicles):
                        tveh = self._last_telemetry.mVehicles[j]
                        if tveh.mID == veh.mID:
                            lap_time_s = tveh.mElapsedTime - tveh.mLapStartET
                            break
                return PlayerLapTiming(
                    lap_time_s=max(0.0, lap_time_s),
                    sector=veh.mSector,
                    cur_sector1=veh.mCurSector1,
                    cur_sector2=veh.mCurSector2,
                    best_sector1=veh.mBestSector1,
                    best_sector2=veh.mBestSector2,
                    best_lap_time=veh.mBestLapTime,
                    last_lap_time=veh.mLastLapTime,
                    count_lap_flag=veh.mCountLapFlag,
                    lap_dist=veh.mLapDist,
                    total_laps=veh.mTotalLaps,
                )
        return None

    def get_player_scoring_raw(self):
        """Diagnostic use only (2026-07-25) - returns the player's raw
        RF2VehicleScoring ctypes object, or None. For probing candidate
        fields that aren't yet exposed through get_player_telemetry()/
        get_player_lap_timing() while we figure out which one actually
        reflects live lap-invalidation (mCountLapFlag didn't - see __main__)."""
        if self._last_scoring is None:
            return None
        scoring_info = self._last_scoring.mScoringInfo
        for i in range(scoring_info.mNumVehicles):
            veh = self._last_scoring.mVehicles[i]
            if veh.mIsPlayer:
                return veh
        return None

    def close(self):
        self._telemetry_block.close()
        self._scoring_block.close()


if __name__ == "__main__":
    # Standalone diagnostic: prints live values to the console, no Nano/serial
    # involved - isolates whether the shared-memory side works on its own.
    #
    # 2026-07-25: also prints several candidate "is this lap invalid" fields
    # side by side - mCountLapFlag==2 was assumed to mean "valid" and
    # mTimeIntoLap was assumed to freeze/reset on an invalid lap the way
    # rFactor2's own HUD does; both were wrong against live testing
    # (countLapFlag sat at 1 constantly, timeIntoLap kept counting through
    # an invalidated lap same as before). Rather than guess a third field
    # one at a time, this dumps mFinishStatus/mNumPenalties/mPathLateral/
    # mTrackEdge/mFlag/mUnderYellow together - drive and deliberately cut a
    # corner, watch which of these actually changes right when the game's
    # own HUD shows its strikethrough, and report back which one moves.
    print(f"sizeof(RF2VehicleTelemetry) = {ctypes.sizeof(RF2VehicleTelemetry)} bytes")
    print(f"sizeof(RF2Telemetry)        = {ctypes.sizeof(RF2Telemetry)} bytes")
    print("Opening rFactor2 shared memory (needs the game running with the plugin loaded)...")
    reader = RF2SharedMemoryReader()
    print("Opened OK. Waiting for player telemetry (enter a session in-game)... Ctrl+C to stop.")

    # 2026-07-25: LapRecorder (volgas_rf2_dash.py) builds its own internal
    # reference curve independent of rF2's own mBestSector*/mBestLapTime
    # fields, so watching those doesn't show whether THIS mechanism is
    # actually working - drive it here directly and print its internal
    # state (sample count, whether a best lap got set) to see why nothing's
    # showing up on the dash.
    from volgas_rf2_dash import LapRecorder
    lap_recorder = LapRecorder()

    try:
        while True:
            reader.poll()
            pt = reader.get_player_telemetry()
            timing = reader.get_player_lap_timing()
            veh = reader.get_player_scoring_raw()
            delta_s = None
            if timing is not None:
                lap_valid = 1 if timing.count_lap_flag == 2 else 0
                lap_recorder.update(timing.lap_dist, timing.lap_time_s, timing.total_laps, lap_valid == 1)
                delta_s = lap_recorder.compute_delta(timing.lap_dist, timing.lap_time_s)
            if pt:
                line = (f"gear={pt.gear:>2} rpm={pt.rpm:7.0f}/{pt.rpm_max:.0f} "
                        f"speed={pt.speed_kph:6.1f}kph lap={pt.lap_number}")
                if timing:
                    line += f" | sector={timing.sector} lapTime={timing.lap_time_s:6.2f} lapDist={timing.lap_dist:7.1f} totalLaps={timing.total_laps}"
                    line += (f" | recSamples={len(lap_recorder._current_samples)} "
                             f"bestSet={lap_recorder._best_samples is not None} "
                             f"bestLapTime={lap_recorder._best_lap_time} "
                             f"delta={delta_s}")
                if veh and reader._last_scoring is not None:
                    # 2026-07-25: two lap-time candidates tried and both
                    # reported wrong against live testing (scoring's
                    # mCurrentET-mLapStartET, then telemetry's
                    # mElapsedTime-mLapStartET) despite both being confirmed
                    # dimensionally correct against the reference header
                    # (docs/rf2_reference/rF2State.h) and correctly-offset
                    # structs. Printing mLapDist ("current distance around
                    # track", confirmed in the header) alongside both time
                    # candidates settles empirically whether either one is
                    # actually tracking distance instead of time: sit
                    # stationary for a few seconds - a genuine time value
                    # keeps climbing at 1.0/sec regardless; a distance value
                    # stays flat while not moving.
                    current_et = reader._last_scoring.mScoringInfo.mCurrentET
                    subtraction = current_et - veh.mLapStartET
                    line += f" subtraction={subtraction:6.2f} lapDist={veh.mLapDist:7.1f}"
                    if reader._last_telemetry is not None:
                        for j in range(reader._last_telemetry.mNumVehicles):
                            tveh = reader._last_telemetry.mVehicles[j]
                            if tveh.mID == veh.mID:
                                line += f" telemSub={tveh.mElapsedTime - tveh.mLapStartET:6.2f}"
                                break
                    line += (f" | countLapFlag={veh.mCountLapFlag} finishStatus={veh.mFinishStatus} "
                             f"numPenalties={veh.mNumPenalties} pathLateral={veh.mPathLateral:6.2f} "
                             f"trackEdge={veh.mTrackEdge:6.2f} flag={veh.mFlag} "
                             f"underYellow={veh.mUnderYellow}")
                print(f"\r{line}   ", end="")
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()
