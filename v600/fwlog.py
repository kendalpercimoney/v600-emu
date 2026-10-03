"""Narrate the boot by hooking the firmware's vsprintf (0x103F919C, (buf, fmt, va_list*))."""
import re

VSPRINTF = 0x103F919C
_SPEC = re.compile(r'%[-+ #0]*\d*(?:\.\d+)?(l|h|ll)?([diuxXcsp%])')


def read_cstr(m, addr, limit=200):
    out = bytearray()
    try:
        while len(out) < limit:
            b = m.uc.mem_read(addr + len(out), 1)[0]
            if b == 0:
                break
            out.append(b)
    except Exception:
        return '<bad ptr>'
    return out.decode('latin1')


def format_c(m, fmt, argp):
    def nxt():
        nonlocal argp
        v = m.read32(argp)
        argp += 4
        return v

    def rep(mt):
        conv = mt.group(2)
        if conv == '%':
            return '%'
        v = nxt()
        if conv in 'di':
            return str(v - (1 << 32) if v & 0x80000000 else v)
        if conv == 'u':
            return str(v)
        if conv in 'xX':
            return format(v, conv)
        if conv == 'p':
            return f'{v:#x}'
        if conv == 'c':
            return chr(v & 0xFF)
        if conv == 's':
            return read_cstr(m, v)
        return mt.group(0)
    try:
        return _SPEC.sub(rep, fmt)
    except Exception as e:
        return fmt + f'  <fmt error {e}>'


def install(m, sink=None, quiet_patterns=()):
    sink = sink or m.log
    m.fw_messages = []

    def hook(m):
        fmt = read_cstr(m, m.r(1))
        try:
            argp = m.read32(m.r(2))
        except Exception:
            argp = 0
        text = format_c(m, fmt, argp).rstrip()
        m.fw_messages.append(text)
        if not any(p in text for p in quiet_patterns):
            sink(f'  [fw] {text}')
    m.hook(VSPRINTF, hook)
