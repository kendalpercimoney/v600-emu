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

Calls. Call control (GSM 04.08 section 5) runs in the services; MM only carries its
messages over an "MM connection" (group MMCM), so the network side here speaks standard
call-control messages:
  0x15001 connection request [protocol (3 = call control), transaction, ...] -> answered
      with 0x15020 [protocol, transaction, 0] (built by MM at 0x1094F808), sent to the port
      MM looks up with 0x10A1D4C4(protocol, transaction).
  0x15000 data to the network [protocol, transaction, length, L3 message]
  0x15040 data from the network [protocol, transaction, length (2 bytes), L3 message]
  0x15003 connection released by the phone.
An outgoing call: SETUP -> CALL PROCEEDING, ALERTING, then CONNECT (answered by CONNECT
ACKNOWLEDGE); DISCONNECT -> RELEASE -> RELEASE COMPLETE. The far end never hangs up.

Signal strength: layer 1 sends 0x11810 (built at 0x108D6884) to the port at 0x121B7020:
  +0 level in percent (0, 10, 30, 50, 70, 90 for 0-5 bars), +1 dBm + 110.
"""
CREATE_MESSAGE = 0x1087BEF4         # (size, id, 0, 0) -> message body
SEND_MESSAGE = 0x10AC73A0           # (message, port, 0); the id is at message - 8
SEND_REG_REQ = 0x100C177A

MNMM_STATUS, MNMM_REG_CNF = 0x14C40, 0x14C41
REGISTERED = 4
SIGNAL_IND, SIGNAL_PORT = 0x11810, 0x121B7020
MMCM_DATA_REQ, MMCM_EST_REQ, MMCM_REL_REQ = 0x15000, 0x15001, 0x15003
MMCM_EST_CNF, MMCM_DATA_IND = 0x15020, 0x15040
MMCM_PORT = 0x10A1D4C4              # (protocol, transaction) -> port of the service
CALL_CONTROL = 3
# call-control message types (GSM 04.08 table 10.3)
ALERTING, CALL_PROCEEDING, SETUP, CONNECT, CONNECT_ACK = 0x01, 0x02, 0x05, 0x07, 0x0F
DISCONNECT, RELEASE, RELEASE_COMPLETE = 0x25, 0x2D, 0x2A
ANSWER_AFTER = 4.0                  # seconds of ringing before the far end answers
SIGNAL = bytes([90, 110 - 65])      # 5 bars, -65 dBm
PLMN = bytes.fromhex('00f110')      # MCC 001, MNC 01: the test network, like the SIM's IMSI
LAC = bytes.fromhex('0001')
REGISTRATION_DELAY = 2.0            # seconds from the request to "registered"


class Network:
    def __init__(self, m, ips):
        self.m = m
        self.ips = ips
        self.port = None                # where MM sends MNMM messages
        self.events = []                # (due insns, fn, tag)
        self.call_port = None
        self.registered = False
        self.from_phone = []            # call-control message types the phone sent (tests)
        m.hook(SEND_MESSAGE, self._sent)
        m.hook(SEND_REG_REQ, self._reg_req)

    def _sent(self, m):
        message = m.r(0)
        ident = m.read32(message - 8)
        if ident == MNMM_STATUS and self.port is None:
            self.port = m.r(1)
        elif ident == MMCM_EST_REQ:
            protocol, transaction = m.uc.mem_read(message, 2)
            if protocol == CALL_CONTROL:
                self.after(0.3, lambda: self._connect(protocol, transaction))
        elif ident == MMCM_DATA_REQ:
            protocol, transaction, n = m.uc.mem_read(message, 3)
            if protocol == CALL_CONTROL:
                self._call_control(transaction, bytes(m.uc.mem_read(message + 3, n)))
        elif ident == MMCM_REL_REQ:
            self.events = [e for e in self.events if e[2] != 'call']

    # -- calls --------------------------------------------------------------------------
    def _connect(self, protocol, transaction):
        """The MM connection is up: tell the service, and remember its port."""
        def found(m, port):
            self.call_port = port
            self.post(MMCM_EST_CNF, bytes([protocol, transaction, 0]), port)
        self.m.call(MMCM_PORT, (protocol, transaction), found)

    def _call_control(self, transaction, l3):
        kind = l3[1] & 0x3F
        self.from_phone.append(kind)
        say = lambda kind, delay: self.after(delay, lambda: self._to_phone(transaction, l3[0], kind), 'call')
        if kind == SETUP:
            say(CALL_PROCEEDING, 0.3)
            say(ALERTING, 1.0)
            say(CONNECT, 1.0 + ANSWER_AFTER)
        elif kind == DISCONNECT:
            self.events = [e for e in self.events if e[2] != 'call']
            say(RELEASE, 0.2)
        elif kind == RELEASE:
            say(RELEASE_COMPLETE, 0.2)

    def _to_phone(self, transaction, first, kind):
        l3 = bytes([first ^ 0x80, kind])            # the other end of the transaction
        body = bytes([CALL_CONTROL, transaction]) + len(l3).to_bytes(2, 'big') + l3
        self.post(MMCM_DATA_IND, body, self.call_port)

    def _reg_req(self, m):
        if m.r(0) == 1:
            self.after(REGISTRATION_DELAY, self.register)

    def register(self):
        self.registered = True
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
    def after(self, seconds, fn, tag=None):
        self.events.append((self.m.insns + int(seconds * self.ips), fn, tag))

    def update(self):
        due = [e for e in self.events if e[0] <= self.m.insns]
        self.events = [e for e in self.events if e[0] > self.m.insns]
        for _, fn, _ in due:
            fn()

    def next_event(self):
        if not self.events:
            return None
        return min(e[0] for e in self.events) * self.m.counter.k
