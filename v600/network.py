"""The pretend GSM network (core/firmware.c): registration, signal, outgoing calls.
Call-control message types (GSM 04.08 table 10.3) for the tests."""
import ctypes

from .devices import _lib

ALERTING, CALL_PROCEEDING, SETUP, CONNECT, CONNECT_ACK = 0x01, 0x02, 0x05, 0x07, 0x0F
DISCONNECT, RELEASE, RELEASE_COMPLETE = 0x25, 0x2D, 0x2A


class Network:
    def __init__(self, ptr):
        self.lib = _lib()
        self.ptr = ptr

    @property
    def registered(self):
        return bool(self.lib.network_registered(self.ptr))

    @property
    def from_phone(self):
        """Call-control message types the phone has sent, in order."""
        buf = (ctypes.c_uint8 * 256)()
        return list(buf[:self.lib.network_from_phone(self.ptr, buf, 256)])
