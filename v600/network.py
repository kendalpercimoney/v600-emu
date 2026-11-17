"""The pretend GSM network (core/firmware.c): registration, signal, calls both ways.
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

    def incoming_call(self, number=''):
        """Someone calls the phone; False if it is not registered or already in a call."""
        return bool(self.lib.network_incoming_call(self.ptr, number.encode()))

    def incoming_sms(self, sender, text):
        """A text message to the phone; False if not registered or one is on its way."""
        return bool(self.lib.network_incoming_sms(self.ptr, sender.encode(), text.encode()))

    @property
    def sms_delivered(self):
        """The phone acknowledged the last text message (RP-ACK)."""
        return bool(self.lib.network_sms_delivered(self.ptr))

    def hang_up(self):
        """The far end hangs up; False if there is no call."""
        return bool(self.lib.network_hang_up(self.ptr))
