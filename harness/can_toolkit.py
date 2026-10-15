# -*- coding: ascii -*-
#
# can_toolkit -- the in-emulator half of the engine.
#
# THIS FILE RUNS INSIDE RENODE, under IronPython 2. It is not host Python and it
# is not importable by the test suite. The dialect constraints are absolute:
#
#     print 'x'            not   print('x')
#     except Exception, e  not   except Exception as e
#     '%s' % x             not   '{}'.format(x)  and never an f-string
#     no pathlib, no typing, no int.from_bytes, no bytes.hex()
#     ASCII only; a non-ASCII byte anywhere here fails at load
#
# A function named mc_<name> becomes the Renode monitor command <name>. That is
# the entire interface: harness/compile.py emits monitor commands, this file
# implements them, and nothing else crosses the boundary.
#
# ---------------------------------------------------------------------------
# WHY THE TAP IS PER NODE AND NOT ON THE HUB
#
# The obvious design is one subscription to the CAN hub, which carries every
# frame on the bus. It was measured and it does not work: with a single node
# attached, hub.FrameTransmitted fired ZERO times while the controller's own
# FrameSent fired for every frame. The hub raises that event when it delivers TO
# a node, so a bus with one node, or any frame nobody is listening for, is
# invisible.
#
# So a send handler is attached to EVERY real node's controller. That is what
# makes expect_no_can trustworthy: an absence assertion on an identifier the
# device under test never sends must still fail when a PEER sends it. Tapping
# only the DUT reports clean while matching traffic flows on the bus, which is a
# silent false pass, and a silent false pass is the worst defect a verification
# tool can have.
# ---------------------------------------------------------------------------

import System
from Antmicro.Renode.Core.CAN import CANMessageFrame

# --------------------------------------------------------------------------
# State. Module level because the monitor calls these as free functions.
# --------------------------------------------------------------------------
_events = []            # list of formatted event-log lines
_taps = {}              # node id -> controller object
_frames = []            # every frame observed: (us, node, id, data tuple)
_matchers = []          # armed assertions not yet resolved
_verdicts = []          # resolved assertions
_log_path = [None]
_engine_errors = []     # anything the toolkit itself could not do


_origin = [0]


def _us():
    """Current VIRTUAL time in microseconds.

    MasterTimeSource, not machine.ElapsedVirtualTime: the latter returns a
    TimeStamp object with no microsecond accessor. Every timestamp this file
    writes is virtual, so it is reproducible across host load and worker count.
    """
    emu = emulationManager.CurrentEmulation
    now = int(emu.MasterTimeSource.ElapsedVirtualTime.TotalMicroseconds)
    return now - _origin[0]


def _absolute_us():
    emu = emulationManager.CurrentEmulation
    return int(emu.MasterTimeSource.ElapsedVirtualTime.TotalMicroseconds)


def mc_ct_reset():
    """Begin a new scenario inside a shared emulator process.

    Batching exists because roughly two thirds of a run is fixed startup. It is a
    CORRECTNESS requirement before it is a speed one: if N scenarios in one
    process give different answers from N processes, the speed is worthless.

    Everything that could carry state across a scenario boundary is cleared here,
    and the time origin is moved to now. Virtual time is global and keeps running
    when machines are removed, so without the origin every scenario after the
    first would carry its predecessors' elapsed time and no batched log could
    match an unbatched one.
    """
    del _events[:]
    del _frames[:]
    del _matchers[:]
    del _verdicts[:]
    del _trace[:]
    del _engine_errors[:]
    # _taps is deliberately NOT cleared. Machines persist across a batch and are
    # reset rather than removed, so the controllers, and the send handlers
    # attached to them, are still the right ones. Clearing the table would leave
    # the next scenario observing nothing, which reports as a clean absence: a
    # silent false pass, and the worst outcome available.
    _origin[0] = _absolute_us()


# --------------------------------------------------------------------------
# SECURITY BOUNDARY -- do not treat this as formatting.
#
# Scenario text (labels, marks) reaches the event log. The event log is parsed
# afterwards to decide PASS or FAIL. A label containing a newline could
# therefore write a line that looks like an observation the emulator made, and
# manufacture a PASS for a scenario that never ran.
#
# Every scenario-supplied string passes through here before it can reach the
# log. Flattening to one line is the boundary; the truncation is secondary.
# --------------------------------------------------------------------------
def _flatten(text):
    if text is None:
        return ''
    s = '%s' % text
    out = []
    for ch in s:
        o = ord(ch)
        if ch in '\r\n\t':
            out.append(' ')
        elif o < 32 or o > 126:
            out.append('?')
        else:
            out.append(ch)
    flat = ''.join(out).strip()
    if len(flat) > 200:
        flat = flat[:197] + '...'
    return flat


def _emit(kind, text):
    _events.append('%10d  %-12s %s' % (_us(), kind, text))


def _data_to_int(data):
    """Frame payload to a little-endian integer.

    int.from_bytes does not exist in this dialect, so it is done by hand. Byte 0
    is the least significant, matching the catalogue's bit numbering.
    """
    value = 0
    shift = 0
    for b in data:
        value |= (int(b) & 0xFF) << shift
        shift += 8
    return value


def _data_hex(data):
    parts = []
    for b in data:
        parts.append('%02X' % (int(b) & 0xFF))
    return ' '.join(parts)


# --------------------------------------------------------------------------
# Taps
# --------------------------------------------------------------------------
def _make_handler(node_id):
    def _handler(frame):
        try:
            data = list(frame.Data)
            entry = (_us(), node_id, int(frame.Id), tuple(data))
            _frames.append(entry)
            _emit('TX', '%-6s 0x%03X  %s' % (node_id, int(frame.Id), _data_hex(data)))
            _feed(entry)
        except Exception, e:
            _engine_errors.append('tap handler on %s: %s' % (node_id, e))
    return _handler


def mc_ct_tap(node_id, machine, peripheral):
    """Attach a send handler to one real node's CAN controller.

    node_id is the LOGICAL name from network.yml and is what appears in the event
    log. machine is the PHYSICAL Renode machine, which in a batch is unique per
    scenario: recreating a machine under a name that has already been used and
    removed crashes the emulator natively, so each scenario gets its own.
    Separating the two keeps a batched event log comparable with an unbatched
    one, which is the whole point of the batching check.
    """
    node_id = _flatten(node_id)
    machine = _flatten(machine)
    try:
        emu = emulationManager.CurrentEmulation
        controller = emu[machine][peripheral]
        controller.FrameSent += _make_handler(node_id)
        _taps[node_id] = controller
        print 'ct: tapped %s (machine %s, %s)' % (node_id, machine, peripheral)
    except Exception, e:
        _engine_errors.append('could not tap %s at %s.%s: %s'
                              % (node_id, machine, peripheral, e))
        print 'ct: FAILED to tap %s at %s.%s: %s' % (node_id, machine, peripheral, e)


# --------------------------------------------------------------------------
# Matchers
#
# An assertion is (value, mask), never an equality. A real status frame carries
# a counter that changes on every transmission; an equality match would fail
# intermittently against frames that are otherwise exactly right.
# --------------------------------------------------------------------------
def _feed(entry):
    us, node, msg_id, data = entry
    payload = _data_to_int(data)
    for m in _matchers:
        # A CLOSED matcher is finished and must stop matching. Checking only
        # 'resolved' was a real defect: a forbid that passes is never marked
        # resolved (nothing hit it), so it stayed live after its window closed
        # and fired FORBID_HIT on the next matching frame. The assertion then
        # appeared twice in the verdict, once passed and once failed, and the
        # scenario failed on an observation from outside its own window.
        if m.get('closed') or m['resolved']:
            continue
        if m['id'] != msg_id:
            continue
        # Frames arriving after the window has elapsed but before settle() runs
        # are outside the assertion's window and must not resolve it.
        if us > m['armed_us'] + m['window_us']:
            continue
        if (payload & m['mask']) != (m['value'] & m['mask']):
            continue
        m['hit_us'] = us
        m['hit_node'] = node
        m['hit_data'] = data
        if m['kind'] == 'expect':
            m['resolved'] = True
            _emit('EXPECT_MET', '%s  (+%d us, from %s)'
                  % (m['label'], us - m['armed_us'], node))
        else:
            m['resolved'] = True
            _emit('FORBID_HIT', '%s  (at +%d us, from %s, 0x%03X %s)'
                  % (m['label'], us - m['armed_us'], node, msg_id, _data_hex(data)))


def mc_ct_expect(msg_id, value, mask, within_ms, label):
    """Require a matching frame within a deadline."""
    m = {
        'kind': 'expect', 'id': int(msg_id), 'value': long(value), 'mask': long(mask),
        'window_us': int(within_ms) * 1000, 'label': _flatten(label),
        'armed_us': _us(), 'resolved': False, 'hit_us': None, 'hit_node': None,
        'hit_data': None,
    }
    _matchers.append(m)
    _emit('EXPECT_ARM', '%s  (0x%03X within %s ms)' % (m['label'], m['id'], within_ms))


def mc_ct_forbid(msg_id, value, mask, for_ms, label):
    """Require the ABSENCE of a matching frame for a window."""
    m = {
        'kind': 'forbid', 'id': int(msg_id), 'value': long(value), 'mask': long(mask),
        'window_us': int(for_ms) * 1000, 'label': _flatten(label),
        'armed_us': _us(), 'resolved': False, 'hit_us': None, 'hit_node': None,
        'hit_data': None,
    }
    _matchers.append(m)
    _emit('FORBID_ARM', '%s  (0x%03X for %s ms)' % (m['label'], m['id'], for_ms))


def mc_ct_settle():
    """Resolve every armed matcher whose window has closed.

    Called by the compiled script after the RunFor that covers the window. An
    expect that never matched, or matched after its deadline, fails here; a
    forbid that was never hit passes here.
    """
    now = _us()
    for m in _matchers:
        if m.get('closed'):
            continue
        if now < m['armed_us'] + m['window_us']:
            continue
        m['closed'] = True
        if m['kind'] == 'expect':
            if m['resolved'] and (m['hit_us'] - m['armed_us']) <= m['window_us']:
                _verdicts.append(('PASS', m))
            else:
                m['resolved'] = True
                _emit('EXPECT_UNMET', '%s  (no matching frame within %d us)'
                      % (m['label'], m['window_us']))
                _verdicts.append(('FAIL', m))
        else:
            if m['resolved']:
                _verdicts.append(('FAIL', m))
            else:
                _emit('FORBID_MET', '%s  (absent for %d us)'
                      % (m['label'], m['window_us']))
                _verdicts.append(('PASS', m))


# --------------------------------------------------------------------------
# Stimulus and bus injection
# --------------------------------------------------------------------------
def mc_ct_mark(text):
    _emit('MARK', _flatten(text))


def mc_ct_stim(node_id, symbol, value):
    """Record a symbol write. The write itself is a sysbus command."""
    _emit('STIM', '%-6s %s = %s' % (_flatten(node_id), _flatten(symbol), _flatten(value)))


def mc_ct_uart(node_id, text):
    _emit('UART', '%-6s %s' % (_flatten(node_id), _flatten(text)))


def mc_ct_inject(target_node, peripheral, msg_id, hex_bytes):
    """Push a frame into a node's receive path, as a peer would.

    Every real node except the sender receives it, so a frame put on the bus is
    seen by the bus and not only by one listener.
    """
    try:
        raw = []
        for part in _flatten(hex_bytes).split():
            raw.append(int(part, 16) & 0xFF)
        data = System.Array[System.Byte](raw)
        frame = CANMessageFrame(int(msg_id), data)
        emu = emulationManager.CurrentEmulation
        target = _flatten(target_node)
        delivered = 0
        for node_id in _taps:
            if node_id == target:
                continue
            _taps[node_id].OnFrameReceived(frame)
            delivered += 1
        _emit('INJ', '%-6s 0x%03X  %s  (delivered to %d node(s))'
              % (target, int(msg_id), _data_hex(raw), delivered))
    except Exception, e:
        _engine_errors.append('inject 0x%s failed: %s' % (msg_id, e))
        print 'ct: inject FAILED: %s' % e


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Console and memory assertions
# --------------------------------------------------------------------------
def mc_ct_uart_expect(node_id, path, text, label):
    """Record a console assertion. It is DECIDED host-side, not here.

    Renode holds the console file backend open for the whole emulation, so
    opening it from inside IronPython raises

        [Errno 32] Sharing violation on path .../uart-<node>.log

    Rather than tap a second, different stream and risk the assertion
    disagreeing with the artefact a person reads, the intent is written to
    the event log and harness/judge.py decides it once the file is closed.
    The fields are pipe separated so the host side can parse them without
    guessing where the label ends.
    """
    _emit('UART_EXPECT', '%s|%s|%s' % (_flatten(node_id), _flatten(text),
                                       _flatten(label)))


_READERS = {1: 'ReadByte', 2: 'ReadWord', 4: 'ReadDoubleWord', 8: 'ReadQuadWord'}


def mc_ct_expect_symbol(node_id, machine, address, size, expected, label):
    """Read a symbol back, to prove an injection actually landed.

    Without this a write that silently went nowhere is indistinguishable from a
    firmware that ignored it, and both look like a defect that was not detected.
    """
    node_id = _flatten(node_id)
    lab = _flatten(label)
    try:
        emu = emulationManager.CurrentEmulation
        sysbus = emu[_flatten(machine)]['sysbus']
        reader = _READERS.get(int(size))
        if reader is None:
            raise Exception('unsupported size %s' % size)
        got = int(getattr(sysbus, reader)(long(address)))
        want = int(expected)
        if got == want:
            _emit('SYMBOL_MET', '%s  (0x%X == %d)' % (lab, long(address), got))
            _verdicts.append(('PASS', {'kind': 'symbol', 'label': lab,
                                        'armed_us': _us(), 'window_us': 0,
                                        'hit_us': _us()}))
        else:
            _emit('SYMBOL_UNMET', '%s  (0x%X is %d, expected %d)'
                  % (lab, long(address), got, want))
            _verdicts.append(('FAIL', {'kind': 'symbol', 'label': lab,
                                        'armed_us': _us(), 'window_us': 0,
                                        'hit_us': None}))
    except Exception, e:
        _engine_errors.append('expect_symbol on %s: %s' % (node_id, e))
        print 'ct: expect_symbol FAILED: %s' % e



# --------------------------------------------------------------------------
# Execution trace
#
# The trace is a sequence of checkpoints, each recording every machine's
# retired-instruction count at a labelled point in the scenario.
#
# Why instruction counts and not a per-block log: this CPU exposes
# SetHookAtBlockBegin, which would give true basic-block granularity, but it
# calls back into IronPython for every translation block. A second of virtual
# time is tens of millions of blocks, so the hook is unusable for whole
# scenarios. GetCurrentInstructionsCount is read natively and costs nothing.
#
# It is also a STRONGER stability probe than comparing event logs. Two runs
# that retire an identical number of instructions between the same two points
# executed the same code; two runs whose logs match could still differ
# underneath. If these counts are reproducible, scheduling jitter is not
# reaching the guest at all.
# --------------------------------------------------------------------------
_trace = []


def _instruction_counts():
    counts = []
    emu = emulationManager.CurrentEmulation
    names = list(emu.Names)
    names.sort()
    for name in names:
        try:
            cpu = emu[name]['sysbus.cpu']
            counts.append((name, long(cpu.GetCurrentInstructionsCount())))
        except Exception, e:
            _engine_errors.append('instruction count for %s: %s' % (name, e))
            counts.append((name, -1))
    return counts


def mc_ct_trace(label):
    """Record a trace checkpoint."""
    _trace.append((_us(), _flatten(label), _instruction_counts()))


def mc_ct_trace_flush(path):
    """Write the trace. Virtual time only; no host quantity appears here."""
    try:
        f = open(_flatten(path), 'w')
        for us, label, counts in _trace:
            parts = []
            for name, n in counts:
                parts.append('%s=%d' % (name, n))
            f.write('%10d  %-40s %s%s' % (us, label, ' '.join(parts), chr(10)))
        f.close()
        print 'ct: wrote %d trace checkpoint(s)' % len(_trace)
    except Exception, e:
        print 'ct: FAILED to write trace: %s' % e


def mc_ct_flush(path):
    """Write the event log. Called once, last."""
    mc_ct_settle()
    p = _flatten(path)
    try:
        f = open(p, 'w')
        for line in _events:
            f.write(line + '\n')
        f.close()
        print 'ct: wrote %d event(s) to %s' % (len(_events), p)
    except Exception, e:
        print 'ct: FAILED to write %s: %s' % (p, e)

    fails = 0
    for outcome, m in _verdicts:
        if outcome == 'FAIL':
            fails += 1
    print 'ct: assertions=%d failed=%d engine_errors=%d' % (
        len(_verdicts), fails, len(_engine_errors))
    for msg in _engine_errors:
        print 'ct: ENGINE ERROR: %s' % msg
    # A single machine-readable line the runner greps for. ENGINE_ERRORS is
    # reported separately from failures so a toolkit fault can never be counted
    # as a firmware verdict.
    print 'CT_RESULT assertions=%d failed=%d engine_errors=%d' % (
        len(_verdicts), fails, len(_engine_errors))


def mc_ct_dump():
    """Print the event log to the console, for interactive debugging."""
    for line in _events:
        print line
