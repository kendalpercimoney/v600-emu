"""The firmware's own log: its vsprintf is hooked (core/firmware.c) and each line goes to
sink (default: the machine's log), unless it contains one of quiet_patterns."""
from .devices import _lib


def install(m, sink=None, quiet_patterns=()):
    m.fw_sink = sink or m.log
    m.quiet = tuple(quiet_patterns)
    _lib().fwlog_install(m.c)
