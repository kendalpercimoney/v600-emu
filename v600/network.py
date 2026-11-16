"""A pretend GSM network, at the boundary between the phone's protocol stack and its
services.

The radio side cannot run: layer 1 is driven by the DSP and its TDMA frame interrupts, and
the DSP's program is mask ROM we do not have. So the stack's mobility management (MM) never
answers. Instead of faking radio measurements for it, this answers the services directly,
with the messages MM would send them (OS messages, posted through Machine.call()).

Registration (service provider DL_SIG_REG_GSM_SP, state table at 0x10412E48):
  MNMM_REG_REQ 0x14C01, built by send_mnmm_reg_req (0x100C177A, r0 = type; type 1 after
      event 0x9370 once the SIM is ready), 12 bytes: +2 type, +8..+11 option flags.
  0x14C40 from MM (handler 0x100C1ED4): service status, +2 state (1 = no service; 0 = list
      of available networks at +4, 4 bytes each), +3 flag. MM sends one at power-up, which
      is how we learn the services' port.
  0x14C41 from MM (handler 0x100C21EA): registration result, +2 status (4: registered at
      home; 2, 3: registered, shown with the roaming triangle; others: not), +3..+5 network
      (MCC/MNC, BCD as in GSM 04.08), +7..+8 location area.
  The idle screen then shows the SIM's service provider name (EF_SPN, sim.py).

Signal strength: layer 1 sends 0x11810 (built at 0x108D6884) to the port at 0x121B7020:
  +0 level in percent (0, 10, 30, 50, 70, 90 for 0-5 bars), +1 dBm + 110.
"""
CREATE_MESSAGE = 0x1087BEF4         # (size, id, 0, 0) -> message body
SEND_MESSAGE = 0x10AC73A0           # (message, port, 0); the id is at message - 8
SEND_REG_REQ = 0x100C177A

MNMM_STATUS, MNMM_REG_CNF = 0x14C40, 0x14C41
REGISTERED = 4
SIGNAL_IND, SIGNAL_PORT = 0x11810, 0x121B7020
SIGNAL = bytes([90, 110 - 65])      # 5 bars, -65 dBm
PLMN = bytes.fromhex('00f110')      # MCC 001, MNC 01: the test network, like the SIM's IMSI
LAC = bytes.fromhex('0001')
REGISTRATION_DELAY = 2.0            # seconds from the request to "registered"


class Network:
    def __init__(self, m, ips):
        self.m = m
        self.ips = ips
        self.port = None                # where MM sends MNMM messages
        self.events = []                # (due insns, fn)
        m.hook(SEND_MESSAGE, self._sent)
        m.hook(SEND_REG_REQ, self._reg_req)

    def _sent(self, m):
        if self.port is None and m.read32(m.r(0) - 8) == MNMM_STATUS:
            self.port = m.r(1)

    def _reg_req(self, m):
        if m.r(0) == 1:
            self.after(REGISTRATION_DELAY, self.register)

    def register(self):
        body = bytearray(0x20)
        body[2] = REGISTERED
        body[3:6] = PLMN
        body[7:9] = LAC
        self.post(MNMM_REG_CNF, bytes(body), self.port)
        self.post(SIGNAL_IND, SIGNAL, self.m.read32(SIGNAL_PORT))

    def post(self, ident, body, port):
        """Send an OS message to the phone's services, as the protocol stack would."""
        def filled(m, message):
            m.uc.mem_write(message, body)
            m.call(SEND_MESSAGE, (message, port, 0))
        self.m.call(CREATE_MESSAGE, (len(body), ident, 0, 0), filled)

    # -- time ---------------------------------------------------------------------------
    def after(self, seconds, fn):
        self.events.append((self.m.insns + int(seconds * self.ips), fn))

    def update(self):
        due = [e for e in self.events if e[0] <= self.m.insns]
        self.events = [e for e in self.events if e[0] > self.m.insns]
        for _, fn in due:
            fn()

    def next_event(self):
        if not self.events:
            return None
        return min(e[0] for e in self.events) * self.m.counter.k
