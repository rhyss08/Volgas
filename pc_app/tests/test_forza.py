"""Forza 'Dash' packet decoding, using a packet built from the same layout."""
import socket
import time

from volgas_forza_telemetry import ForzaDashPacket, ForzaUDPReader


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def send(port, data):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(data, ("127.0.0.1", port))
    s.close()


def poll_until(reader, ok, timeout=1.0):
    end = time.time() + timeout
    while time.time() < end:
        reader.poll()
        if ok():
            return True
        time.sleep(0.01)
    return False


def test_decodes_racing_packet():
    port = free_udp_port()
    reader = ForzaUDPReader(port=port, bind_addr="127.0.0.1")
    try:
        pkt = ForzaDashPacket()
        pkt.IsRaceOn, pkt.CurrentEngineRpm, pkt.EngineMaxRpm, pkt.Speed, pkt.Gear = 1, 6500.0, 8000.0, 25.0, 4
        send(port, bytes(pkt))
        assert poll_until(reader, lambda: reader.get_player_telemetry() is not None)
        pt = reader.get_player_telemetry()
        assert (pt.gear, round(pt.rpm), round(pt.rpm_max), round(pt.speed_kph)) == (4, 6500, 8000, 90)
    finally:
        reader.close()


def test_ignores_wrong_sized_packets_and_menus():
    port = free_udp_port()
    reader = ForzaUDPReader(port=port, bind_addr="127.0.0.1")
    try:
        send(port, b"\x00" * 311)        # another Forza format - must not be misread
        pkt = ForzaDashPacket()
        pkt.IsRaceOn = 0                 # paused / in a menu
        send(port, bytes(pkt))
        time.sleep(0.05)
        reader.poll()
        assert reader.get_player_telemetry() is None
    finally:
        reader.close()
