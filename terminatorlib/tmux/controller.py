"""Tmux controller - maps tmux state to Terminator widgets.

One instance per tmux session. Manages the tmux protocol connection,
terminal registration, key translation, and resize handling.
"""

import threading

from gi.repository import Gdk, GLib

from terminatorlib.util import dbg
from terminatorlib.tmux.protocol import TmuxProtocol, TmuxProtocolFromPty
from terminatorlib.tmux.layout import parse_tmux_layout, build_terminator_layout


ESCAPE_CODE = '\033'


def esc(seq):
    return '{}{}'.format(ESCAPE_CODE, seq)


# Map Gdk keysyms to escape sequences for tmux send-keys
# Standard xterm escape sequences for keys that Gdk.keyval_to_unicode
# can't translate (returns 0). Everything else we get from Gdk directly.
XTERM_KEYS = {
    Gdk.KEY_Up: b'\x1b[A',
    Gdk.KEY_Down: b'\x1b[B',
    Gdk.KEY_Right: b'\x1b[C',
    Gdk.KEY_Left: b'\x1b[D',
    Gdk.KEY_Home: b'\x1b[H',
    Gdk.KEY_End: b'\x1b[F',
    Gdk.KEY_Insert: b'\x1b[2~',
    Gdk.KEY_Page_Up: b'\x1b[5~',
    Gdk.KEY_Page_Down: b'\x1b[6~',
    Gdk.KEY_ISO_Left_Tab: b'\x1b[Z',
    Gdk.KEY_F1: b'\x1bOP',
    Gdk.KEY_F2: b'\x1bOQ',
    Gdk.KEY_F3: b'\x1bOR',
    Gdk.KEY_F4: b'\x1bOS',
    Gdk.KEY_F5: b'\x1b[15~',
    Gdk.KEY_F6: b'\x1b[17~',
    Gdk.KEY_F7: b'\x1b[18~',
    Gdk.KEY_F8: b'\x1b[19~',
    Gdk.KEY_F9: b'\x1b[20~',
    Gdk.KEY_F10: b'\x1b[21~',
    Gdk.KEY_F11: b'\x1b[23~',
    Gdk.KEY_F12: b'\x1b[24~',
}

ARROW_KEYS = {Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Left, Gdk.KEY_Right}

MOUSE_WHEEL = {
    Gdk.ScrollDirection.UP: 'C-y C-y C-y',
    Gdk.ScrollDirection.DOWN: 'C-e C-e C-e',
}


_controllers = []  # all active TmuxController instances


def get_controller(terminal):
    """Look up the TmuxController that owns a terminal."""
    ctrl = getattr(terminal, '_tmux_controller', None)
    if ctrl:
        return ctrl
    # Fallback: search all controllers
    for c in _controllers:
        if terminal in c.terminal_to_pane:
            return c
    return None


class TmuxController:
    """Controller bridging tmux and Terminator.

    One instance per tmux session. Use get_controller(terminal) to
    look up the controller for a given terminal widget.
    """

    def __init__(self):
        self.active = False
        self.pane_to_terminal = {}
        self.terminal_to_pane = {}
        self.pane_alternate = {}
        self.window_layouts = {}
        self.window_names = {}
        self.window_indices = {}
        self._last_pane_sizes = {}
        self._prev_vte_sizes = {}
        self._applying_layout = False
        self._layout_clear_scheduled = False
        self._layout_applied_time = 0
        self._resize_timer = None
        self._refresh_client_in_flight = False
        self._pending_tripwire_hit = False
        self._tmux_max_cols = None   # per-axis max from tmux rejection
        self._tmux_max_rows = None
        self._tripwire_timer = None
        self._tripwire_armed = False
        self._tripwire_pixels = None
        self._initial_layout_ready = threading.Event()
        self.session_name = None
        self.protocol = None
        self.handlers = None
        self._last_client_size = None
        self._last_window_pixels = None
        self._window_resize_pending = False  # True after window.resize() until WM responds
        self._configure_handler_id = None
        self._last_chrome = None  # (w, h) content-term chrome
        self.active_window_id = None  # @id of tmux's active window
        self._pending_output = {}  # pane_id -> [data, ...] for pre-registration %output
        self._origin_terminal = None  # terminal that ran tmux -CC (PTY takeover)

    def start(self, session_name, new_session=False):
        """Start the tmux controller.

        Creates subprocess, wires handlers, starts reader,
        queries initial state, and waits for the initial layout
        to be ready before returning.
        """
        self.session_name = session_name
        self._initial_layout_ready = threading.Event()
        self.protocol = TmuxProtocol(session_name, new_session=new_session)

        # Import handlers here to avoid circular imports
        from terminatorlib.tmux.handlers import TmuxHandlers
        self.handlers = TmuxHandlers(self)

        self.protocol.start()
        self.active = True
        _controllers.append(self)
        dbg('TmuxController: started for session %s' % session_name)

        # Query initial state and wait for it
        self._query_initial_state()
        dbg('TmuxController: waiting for initial layout...')
        self._initial_layout_ready.wait(timeout=5.0)
        if not self._initial_layout_ready.is_set():
            dbg('TmuxController: timeout waiting for initial layout')
        else:
            dbg('TmuxController: initial layout ready')

    def start_from_pty(self, pty_fd, session_name=None):
        """Start the tmux controller using an existing PTY fd.

        Used when the user runs 'tmux -CC' inside a terminal and we
        take over the PTY as the communication channel.
        """
        self.session_name = session_name or 'unknown'
        self._initial_layout_ready = threading.Event()
        self.protocol = TmuxProtocolFromPty(pty_fd)

        from terminatorlib.tmux.handlers import TmuxHandlers
        self.handlers = TmuxHandlers(self)

        self.protocol.start()
        self.active = True
        _controllers.append(self)
        dbg('TmuxController: started from PTY fd %d' % pty_fd)

        # Query initial state and wait for it
        self._query_initial_state()
        dbg('TmuxController: waiting for initial layout...')
        self._initial_layout_ready.wait(timeout=5.0)
        if not self._initial_layout_ready.is_set():
            dbg('TmuxController: timeout waiting for initial layout')
        else:
            dbg('TmuxController: initial layout ready')

    def stop(self, send_detach=True):
        """Detach from tmux and clean up.

        send_detach: send detach command to tmux before stopping.
            False when tmux already exited (would go to the shell).
        """
        # Restore termios immediately on the bridge fd before it's closed,
        # so the shell gets correct terminal state when it resumes
        if self.protocol and hasattr(self.protocol, '_bridge'):
            self.protocol._bridge.restore_termios()
        if self.protocol:
            self.protocol.stop(send_detach=send_detach)
        self._refresh_client_in_flight = False
        self.active = False
        # Restore the original PTY on the terminal that ran tmux -CC
        if self._origin_terminal:
            GLib.idle_add(self._restore_origin_terminal)
        # Clear controller reference on terminals
        for terminal in list(self.terminal_to_pane.keys()):
            terminal._tmux_controller = None
        self.pane_to_terminal.clear()
        self.terminal_to_pane.clear()
        self.pane_alternate.clear()
        self.window_layouts.clear()
        self.window_indices.clear()
        if self in _controllers:
            _controllers.remove(self)
        dbg('TmuxController: stopped')

    def _restore_origin_terminal(self):
        """Restore the original PTY on the terminal that started tmux -CC."""
        terminal = self._origin_terminal
        self._origin_terminal = None
        saved = getattr(terminal, '_saved_pty', None)
        if saved and hasattr(terminal, 'vte') and terminal.vte:
            dbg('TmuxController: restoring original PTY on origin terminal')
            terminal._cleanup_tmux_origin()
            terminal.vte.set_pty(saved)
            terminal._saved_pty = None
            # Delay reconnecting tmux detection so VTE can consume any
            # buffered control mode output (like %exit) first
            if not getattr(terminal, '_tmux_detect_id', None):
                GLib.timeout_add(500, self._reconnect_tmux_detect, terminal)
        return False

    def _reconnect_tmux_detect(self, terminal):
        """Reconnect tmux detection after VTE has consumed buffered data."""
        if not hasattr(terminal, 'vte') or not terminal.vte:
            return False
        _col, row = terminal.vte.get_cursor_position()
        terminal._tmux_detect_watermark = row
        terminal._tmux_detect_id = terminal.vte.connect(
            'contents-changed',
            terminal.on_vte_contents_changed_tmux_detect)
        return False  # don't repeat
        return False

    def _query_initial_state(self):
        """Query tmux for current windows and panes."""
        # First learn the session name if we don't know it
        if self.session_name == 'unknown':
            self.protocol.send_command(
                'display-message -p "#{session_name}"',
                callback=self._on_session_name,
            )
        else:
            self._query_windows()

    def _on_session_name(self, result):
        """Handle session name query response."""
        if not result.is_error and result.output_lines:
            name = result.output_lines[0].decode('utf-8', errors='replace').strip()
            if name:
                self.session_name = name
                dbg('TmuxController: session name is %s' % name)
        self._query_windows()

    def _query_windows(self):
        """Query tmux for window layouts and names.
        Uses colon separator to avoid brace-related PTY echo issues.
        """
        self.protocol.send_command(
            'list-windows -F "W:#{window_id}:#{window_index}:#{window_name}:#{window_active}:#{window_layout}"',
            callback=self.handlers.on_initial_list_windows,
        )

    def register_terminal(self, pane_id, terminal):
        """Register a terminal widget for a tmux pane."""
        self.pane_to_terminal[pane_id] = terminal
        self.terminal_to_pane[terminal] = pane_id
        terminal._tmux_controller = self
        # Tmux controls content and sends fresh output on resize,
        # so disable VTE's own text rewrapping to avoid flicker
        # (lines briefly doubling from wrap before new content arrives).
        if hasattr(terminal, 'vte') and terminal.vte:
            terminal.vte.set_rewrap_on_resize(False)
        dbg('TmuxController: registered terminal for pane %s' % pane_id)

        # Replay any %output that arrived before registration
        pending = self._pending_output.pop(pane_id, None)
        if pending:
            dbg('TmuxController: replaying %d buffered chunks for pane %s' % (len(pending), pane_id))
            for data in pending:
                GLib.idle_add(self._replay_output, terminal, data)

    def _replay_output(self, terminal, data):
        """Feed buffered output to terminal VTE. Called on GTK thread."""
        try:
            if hasattr(terminal, 'vte') and terminal.vte:
                terminal.vte.feed(data)
        except Exception as e:
            dbg('TmuxController: replay feed error: %s' % e)
        return False

    def unregister_terminal(self, terminal):
        """Unregister a terminal widget."""
        pane_id = self.terminal_to_pane.pop(terminal, None)
        if pane_id:
            self.pane_to_terminal.pop(pane_id, None)
            self.pane_alternate.pop(pane_id, None)
            terminal._tmux_controller = None
            dbg('TmuxController: unregistered terminal for pane %s' % pane_id)

    def send_keypress(self, terminal, event):
        """Translate a Gdk key event to raw bytes and send via send-keys -H."""
        pane_id = self.terminal_to_pane.get(terminal)
        if not pane_id:
            return

        keyval = event.keyval
        state = event.state
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)

        # Skip Ctrl+Shift+Alt combos
        if alt and ctrl and shift:
            return

        raw = None

        if keyval in XTERM_KEYS:
            raw = XTERM_KEYS[keyval]
            # Ctrl+arrow: modify to CSI 1;5 X
            if ctrl and keyval in ARROW_KEYS:
                raw = b'\x1b[1;5' + raw[-1:]
            elif shift and keyval in ARROW_KEYS:
                raw = b'\x1b[1;2' + raw[-1:]
        else:
            # Use Gdk to get the unicode codepoint
            uc = Gdk.keyval_to_unicode(keyval)
            if uc:
                if ctrl and not alt:
                    # Ctrl+letter: produce control character (e.g. Ctrl-U = 0x15)
                    if 0x40 <= uc <= 0x7e:
                        raw = bytes([uc & 0x1f])
                    elif 0x60 <= uc <= 0x7e:
                        raw = bytes([uc & 0x1f])
                    else:
                        raw = chr(uc).encode('utf-8')
                else:
                    raw = chr(uc).encode('utf-8')
            else:
                return

        if raw is None:
            return

        # Alt prefix: ESC before the key bytes
        if alt and not ctrl:
            raw = b'\x1b' + raw

        # Send as hex via send-keys -H
        hex_str = ' '.join('%02x' % b for b in raw)
        self.protocol.send_command(
            'send-keys -H -t {} {}'.format(pane_id, hex_str))

    def send_paste(self, terminal, text):
        """Send pasted text to tmux as hex-encoded bytes."""
        pane_id = self.terminal_to_pane.get(terminal)
        if not pane_id or not text:
            return
        raw = text.encode('utf-8')
        hex_str = ' '.join('%02x' % b for b in raw)
        self.protocol.send_command(
            'send-keys -H -t {} {}'.format(pane_id, hex_str))

    def send_mousewheel(self, terminal, event):
        """Handle mouse scroll in tmux mode.

        Only active when alternate screen is on (e.g. in vim/less).
        Returns True if handled, False to let Terminator handle it.
        """
        pane_id = self.terminal_to_pane.get(terminal)
        if not pane_id:
            return False

        if not self.pane_alternate.get(pane_id):
            return False

        if event.direction == Gdk.ScrollDirection.SMOOTH:
            if event.delta_y <= 0.0:
                wheel = MOUSE_WHEEL[Gdk.ScrollDirection.UP]
            else:
                wheel = MOUSE_WHEEL[Gdk.ScrollDirection.DOWN]
        elif event.direction in MOUSE_WHEEL:
            wheel = MOUSE_WHEEL[event.direction]
        else:
            return False

        self.protocol.send_command('send-keys -t {} {}'.format(pane_id, wheel))
        return True

    def notify_resize(self, terminal, cols, rows):
        """Notify tmux of terminal resize.

        Distinguishes between window resize (sends refresh-client -C)
        and split bar drag (sends relative resize-pane commands).
        Called immediately on each VTE size-allocate — the char-size
        dedup (size != _last_client_size) prevents redundant commands.
        """
        pane_id = self.terminal_to_pane.get(terminal, '?')
        # Ignore resizes before initial layout has been applied
        if not self._layout_applied_time:
            return
        dbg('notify_resize: %s %dx%d applying=%s' % (
            pane_id, cols, rows, self._applying_layout))
        # Don't send resize while we're applying a layout from tmux.
        # Clear the flag reactively via _finish_applying_layout at
        # priority 210 — this runs after ALL pending notify_resize
        # callbacks (priority 200) have been suppressed.
        if self._applying_layout:
            dbg('notify_resize: suppressed (applying_layout'
                ' resize_pending=%s)' %
                self._window_resize_pending)
            if self.handlers and not self._window_resize_pending:
                # Window has reached target size (or no resize
                # was requested).  Reschedule on every suppressed
                # call — defers clearing until after the LAST VTE
                # settles, including anchor corrections from
                # STALE paneds catching up to their real size.
                from gi.repository import GLib
                src = getattr(self, '_layout_clear_source',
                              None)
                if src:
                    GLib.source_remove(src)
                tree = getattr(self.handlers,
                               '_pending_layout_tree', None)
                self._layout_clear_source = GLib.idle_add(
                    self._do_finish_applying_layout, tree,
                    priority=GLib.PRIORITY_DEFAULT_IDLE + 10)
            return

        def do_resize():
            # Always snapshot current VTE sizes first, so _prev_vte_sizes
            # stays current even when we suppress sending commands
            def _snapshot_vte_sizes():
                for t, pane_id in self.terminal_to_pane.items():
                    try:
                        self._prev_vte_sizes[pane_id] = (
                            t.vte.get_column_count(), t.vte.get_row_count())
                    except Exception:
                        pass

            # Check for chrome change (tab bar appeared/disappeared)
            import time as _time
            for t in self.terminal_to_pane:
                try:
                    top = t.get_toplevel()
                    if self.handlers and self._last_chrome is not None:
                        chrome = self.handlers._get_chrome_size(top)
                        if chrome != self._last_chrome:
                            delta_w = chrome[0] - self._last_chrome[0]
                            delta_h = chrome[1] - self._last_chrome[1]
                            ws = top.get_size()
                            new_w = ws[0] + delta_w
                            new_h = ws[1] + delta_h
                            dbg('size_trace chrome_changed: '
                                '%dx%d -> %dx%d delta=%dx%d '
                                'resize=%dx%d' % (
                                self._last_chrome[0],
                                self._last_chrome[1],
                                chrome[0], chrome[1],
                                delta_w, delta_h,
                                new_w, new_h))
                            top.resize(new_w, new_h)
                            self._last_chrome = chrome
                            self._layout_applied_time = \
                                _time.monotonic()
                            _snapshot_vte_sizes()
                            return False
                    break
                except Exception:
                    pass

            # Detect if the overall window changed size (vs just a split drag)
            window_resized = False
            tripwire_hit = False
            for t in self.terminal_to_pane:
                try:
                    top = t.get_toplevel()
                    px = top.get_size()
                    if px != self._last_window_pixels:
                        dbg('window pixels changed %s -> %s' % (self._last_window_pixels, px))
                        window_resized = True
                        self._last_window_pixels = px
                    # Check if we hit the tripwire boundary
                    # Tripwire is in allocation space (includes CSD)
                    alloc = top.get_allocation()
                    apx = (alloc.width, alloc.height)
                    if (self._tripwire_armed and self._tripwire_pixels
                            and apx):
                        hit_w = (self._tmux_max_cols is not None
                                 and apx[0] >= self._tripwire_pixels[0])
                        hit_h = (self._tmux_max_rows is not None
                                 and apx[1] >= self._tripwire_pixels[1])
                        if hit_w or hit_h:
                            tripwire_hit = True
                    break
                except Exception:
                    pass

            # Log all widget layers to understand chrome
            for t in self.terminal_to_pane:
                try:
                    top = t.get_toplevel()
                    ws = top.get_size()
                    wa = top.get_allocation()
                    va = t.vte.get_allocation()
                    vc = t.vte.get_column_count()
                    vr = t.vte.get_row_count()
                    ta = t.get_allocation()
                    sf = top.get_scale_factor()
                    content = top.get_child()
                    ca = content.get_allocation() if content else None
                    rp = self.handlers._find_root_paned(t) \
                        if self.handlers else None
                    pa = rp.get_allocation() if rp else None
                    cw = t.vte.get_char_width()
                    ch = t.vte.get_char_height()
                    dbg('do_resize layers: '
                        'scale=%d ws=%dx%d alloc=%dx%d '
                        'content=%s paned=%s '
                        'term=%dx%d vte=%dx%d '
                        'chars=%dx%d char_px=%dx%d' % (
                        sf,
                        ws[0], ws[1], wa.width, wa.height,
                        '%dx%d' % (ca.width, ca.height)
                            if ca else 'None',
                        '%dx%d' % (pa.width, pa.height)
                            if pa else 'None',
                        ta.width, ta.height,
                        va.width, va.height,
                        vc, vr, cw, ch))
                    if ca and pa:
                        dbg('do_resize chrome: '
                            'content-paned=%dx%d '
                            'ws-vte=%dx%d '
                            'paned-vte=%dx%d '
                            'term-vte=%dx%d' % (
                            ca.width - pa.width,
                            ca.height - pa.height,
                            ws[0] - va.width,
                            ws[1] - va.height,
                            pa.width - va.width,
                            pa.height - va.height,
                            ta.width - va.width,
                            ta.height - va.height))
                    elif ca:
                        dbg('do_resize chrome: '
                            'NO PANED '
                            'content-term=%dx%d '
                            'ws-vte=%dx%d '
                            'term-vte=%dx%d' % (
                            ca.width - ta.width,
                            ca.height - ta.height,
                            ws[0] - va.width,
                            ws[1] - va.height,
                            ta.width - va.width,
                            ta.height - va.height))
                    break
                except Exception:
                    pass
            dbg('notify_resize: window_resized=%s pane_count=%d' % (
                window_resized, len(self.terminal_to_pane)))
            if window_resized or len(self.terminal_to_pane) <= 1:
                # Window resize: send refresh-client -C with total size
                if not self._refresh_client_in_flight:
                    total_cols, total_rows = self._calculate_client_size()
                    if total_cols > 0 and total_rows > 0:
                        size = (total_cols, total_rows)
                        if tripwire_hit:
                            self._pending_tripwire_hit = True
                        if size != self._last_client_size:
                            self._last_client_size = size
                            self._refresh_client_in_flight = True
                            dbg('sending refresh-client -C %d,%d'
                                % (total_cols, total_rows))
                            self.protocol.send_command(
                                'refresh-client -C {},{}'.format(
                                    total_cols, total_rows))
                            self._refresh_layout_state(
                                callback=self._on_refresh_complete)
                            if (self.handlers and
                                    self.handlers._initial_capture_pending):
                                self.handlers._initial_capture_pending = False
                                self.handlers._send_initial_captures()
                else:
                    dbg('notify_resize: skipped (refresh in flight)')
            else:
                # Split bar drag: send absolute resize for the most-changed pane
                self._send_split_bar_resize()

            _snapshot_vte_sizes()
            return False

        do_resize()

    def _do_finish_applying_layout(self, tree):
        """Clear _applying_layout after all VTE size-allocate callbacks.

        Scheduled at priority DEFAULT_IDLE+10 (210) from notify_resize.
        Rescheduled on every suppressed notify_resize, so it only
        fires after the LAST VTE settles (including anchor cascades).
        """
        self._layout_clear_source = None
        if self.handlers:
            self.handlers._finish_applying_layout(tree)
        return False

    def _ensure_configure_handler(self, window):
        """Connect configure-event handler once per window."""
        if not self._configure_handler_id:
            self._configure_handler_id = window.connect(
                'configure-event',
                self._on_configure_event)

    def _on_configure_event(self, window, event):
        """WM responded to a window resize request.

        Clears _window_resize_pending so the suppressed notify_resize
        path can schedule _finish_applying_layout.  The allocation
        cascade from this configure-event will trigger VTE
        size-allocate → notify_resize → idle schedule.
        """
        ws = window.get_size()
        wa = window.get_allocation()
        if self._window_resize_pending:
            self._window_resize_pending = False
            self._last_window_pixels = ws
            dbg('configure-event: resize complete, '
                'size=%s alloc=%dx%d applying=%s' % (
                ws, wa.width, wa.height,
                self._applying_layout))
        else:
            dbg('configure-event: unsolicited, '
                'size=%s alloc=%dx%d applying=%s '
                'last_px=%s' % (
                ws, wa.width, wa.height,
                self._applying_layout,
                self._last_window_pixels))
        return False  # propagate

    def _do_arm_tripwire(self):
        """Set max to the next character boundary so we can detect
        when the user tries to grow past the current max.
        Only constrains axes that have a known limit; unconstrained
        axes get screen-max so the user can freely resize them."""
        if (self._tmux_max_cols is None and self._tmux_max_rows is None) \
                or not self.handlers:
            # No constraints — clear any stale MAX hint
            if self.handlers:
                self.handlers._clear_tmux_max_size()
            return False
        # For constrained axes, probe +1 char beyond the limit.
        # For unconstrained axes, use current tree size (no limit).
        tree = None
        for tree in self.handlers._layout_trees.values():
            break
        if tree is None:
            return False
        probe_cols = (self._tmux_max_cols + 1) if self._tmux_max_cols \
            else tree.width
        probe_rows = (self._tmux_max_rows + 1) if self._tmux_max_rows \
            else tree.height
        info_next = self.handlers._chars_to_max_pixels(
            probe_cols, probe_rows)
        if not info_next:
            return False
        trip_w, trip_h = info_next[0], info_next[1]
        # For unconstrained axes, use a very large value so
        # the WM doesn't limit that axis and tripwire never fires.
        max_w = trip_w if self._tmux_max_cols is not None else 32767
        max_h = trip_h if self._tmux_max_rows is not None else 32767
        # _set_max_size_pixels adds CSD → allocation-space values
        hint_w, hint_h = self.handlers._set_max_size_pixels(max_w, max_h)
        dbg('arming tripwire: cols=%s rows=%s '
            'trip=%dx%d px (hint=%dx%d)' % (
                self._tmux_max_cols or 'free',
                self._tmux_max_rows or 'free',
                max_w, max_h, hint_w, hint_h))
        # Store in allocation space to match get_allocation() comparison
        self._tripwire_pixels = (hint_w, hint_h)
        self._tripwire_armed = True
        return False

    def _arm_tripwire_after_idle(self):
        """Arm the tripwire after 2s of idle (initial or rejection)."""
        if self._tripwire_timer:
            GLib.source_remove(self._tripwire_timer)

        def _arm():
            self._tripwire_timer = None
            return self._do_arm_tripwire()

        self._tripwire_timer = GLib.timeout_add(2000, _arm)

    def _send_split_bar_resize(self):
        """Send resize-pane for child1 of the dragged handle.

        Identifies which paned handle the user dragged by finding the
        paned whose position changed but length stayed the same.
        Then targets child1 of that paned — this ensures tmux adjusts
        the correct border (between child1 and its next sibling)
        rather than taking space from a distant pane.
        """
        # Log all pane deltas for debugging
        for terminal, pane_id in self.terminal_to_pane.items():
            try:
                cur = (terminal.vte.get_column_count(),
                       terminal.vte.get_row_count())
                prev = self._prev_vte_sizes.get(pane_id)
                if prev and cur != prev:
                    dbg('split drag delta: %s prev=%dx%d '
                        'cur=%dx%d' % (
                        pane_id, prev[0], prev[1],
                        cur[0], cur[1]))
            except Exception:
                pass

        # Find the dragged handle: position changed, length same
        paneds = getattr(self.handlers, '_tmux_paneds', set())
        dragged = None
        for paned in paneds:
            synced = getattr(paned, '_tmux_synced_pos', None)
            if synced is None:
                continue
            cur_pos = paned.get_position()
            cur_len = paned.get_length()
            prev_len = getattr(paned, '_tmux_prev_len', cur_len)
            child1_id = getattr(
                paned, '_tmux_child1_pane_id', '?')
            dbg('split drag check: child1=%s pos=%d '
                'synced=%d len=%d prev_len=%d' % (
                child1_id, cur_pos, synced,
                cur_len, prev_len))
            if cur_pos != synced and cur_len == prev_len:
                dragged = paned
                break

        # Keep prev_len current so the next check can detect
        # handle drags (position changed, length same).
        for p in paneds:
            p._tmux_prev_len = p.get_length()

        if dragged is None:
            dbg('split drag: no dragged handle found')
            return

        child1_id = getattr(dragged, '_tmux_child1_pane_id',
                            None)
        if child1_id is None:
            dbg('split drag: no child1 pane_id')
            return

        terminal = self.pane_to_terminal.get(child1_id)
        if terminal is None:
            dbg('split drag: terminal not found for %s'
                % child1_id)
            return

        try:
            cur_cols = terminal.vte.get_column_count()
            cur_rows = terminal.vte.get_row_count()
        except Exception:
            return

        prev = self._prev_vte_sizes.get(child1_id)
        if prev is None:
            return

        prev_cols, prev_rows = prev
        dcols = abs(cur_cols - prev_cols)
        drows = abs(cur_rows - prev_rows)

        if dcols == 0 and drows == 0:
            dbg('split drag: child1 %s unchanged (%dx%d)'
                % (child1_id, cur_cols, cur_rows))
            return

        import time as _time
        parts = ['resize-pane -t {}'.format(child1_id)]
        if dcols > 0:
            parts.append('-x {}'.format(cur_cols))
        if drows > 0:
            parts.append('-y {}'.format(cur_rows))
        cmd = ' '.join(parts)
        dbg('split drag: %s (child1 of dragged handle)' % cmd)
        self.protocol.send_command(cmd)
        self._layout_applied_time = _time.monotonic()
        self._refresh_layout_state()

    def _refresh_layout_state(self, callback=None):
        """Send list-windows to refresh our layout tree after a resize."""
        self.protocol.send_command(
            'list-windows -F "W:#{window_id}:#{window_index}:#{window_name}:#{window_active}:#{window_layout}"',
            callback=callback or self.handlers.on_initial_list_windows,
        )

    def _on_refresh_complete(self, result):
        """Reader thread: list-windows response arrived.

        At this point tmux has processed our refresh-client -C.
        If a %layout-change was emitted, on_layout_change already
        ran (reader thread processes sequentially) and set
        _layout_change_pending before queuing _update_pane_sizes.
        """
        self.handlers.on_initial_list_windows(result)
        GLib.idle_add(self._on_refresh_round_trip_done)

    def _on_refresh_round_trip_done(self):
        """GTK thread: refresh-client round-trip is complete.

        Checks state flags to decide whether to clear _in_flight
        now or let _finish_applying_layout do it later.
        """
        if (self._applying_layout
                or getattr(self.handlers,
                           '_layout_change_pending', False)):
            # Layout application is in progress or about to start.
            # _finish_applying_layout will clear _in_flight.
            dbg('round_trip_done: deferring to layout application')
            return False

        # No layout change from our request — clear gate and recheck.
        self._refresh_client_in_flight = False
        self._process_tripwire()
        dbg('round_trip_done: cleared in_flight, rechecking')

        # Trigger one notify_resize to detect if window moved
        # during the round-trip and send a fresh request.
        for t in list(self.terminal_to_pane.keys())[:1]:
            try:
                self.notify_resize(
                    t, t.vte.get_column_count(),
                    t.vte.get_row_count())
            except Exception:
                pass
            break
        return False

    def _process_tripwire(self):
        """Process any tripwire hit that was deferred during in-flight."""
        if self._pending_tripwire_hit:
            self._pending_tripwire_hit = False
            self._tripwire_armed = False
            self._tripwire_pixels = None

    def _pane_size_for_tmux(self, terminal):
        """Get the tmux pane size for a terminal. Returns exact VTE size."""
        return terminal.vte.get_column_count(), terminal.vte.get_row_count()

    def _debug_terminal_sizes(self, terminal, pane_id):
        """Log all pixel and character measurements for a terminal."""
        vte = terminal.vte
        char_w = vte.get_char_width()
        char_h = vte.get_char_height()
        vte_alloc = vte.get_allocation()
        term_alloc = terminal.get_allocation()
        vte_cols = vte.get_column_count()
        vte_rows = vte.get_row_count()

        tb_h = 0
        sb_w = 0
        if hasattr(terminal, 'titlebar') and terminal.titlebar and terminal.titlebar.get_visible():
            tb_h = terminal.titlebar.get_allocation().height
        if hasattr(terminal, 'scrollbar') and terminal.scrollbar and terminal.scrollbar.get_visible():
            sb_w = terminal.scrollbar.get_allocation().width

        dbg('DEBUG %s: cell=%dx%d vte_px=%dx%d vte_chars=%dx%d '
            'term_px=%dx%d titlebar_h=%d scrollbar_w=%d' % (
            pane_id, char_w, char_h,
            vte_alloc.width, vte_alloc.height, vte_cols, vte_rows,
            term_alloc.width, term_alloc.height, tb_h, sb_w))

    def _calculate_client_size(self):
        """Calculate the total tmux client size from VTE grid sizes.

        Sums individual VTE column/row counts using the layout tree
        structure, adding 1 character per tmux separator between panes.
        This avoids the bounding-box approach which inflates the count
        by including scrollbar and handle pixels in the character total.
        """
        terminals = list(self.terminal_to_pane.keys())
        if not terminals:
            return 0, 0

        if len(terminals) == 1:
            t = terminals[0]
            try:
                return t.vte.get_column_count(), t.vte.get_row_count()
            except Exception:
                return 0, 0

        # Use layout tree to sum VTE sizes + 1 per tmux separator
        if self.handlers and self.handlers._layout_trees:
            for tree in self.handlers._layout_trees.values():
                cols, rows = self._sum_vte_sizes(tree)
                if cols > 0 and rows > 0:
                    dbg('client size: %dx%d (from VTE grid + separators)' % (
                        cols, rows))
                    return cols, rows

        # Fallback: single terminal sizes
        t = terminals[0]
        try:
            return t.vte.get_column_count(), t.vte.get_row_count()
        except Exception:
            return 0, 0

    def _sum_vte_sizes(self, node):
        """Compute total character size from actual VTE widgets + tmux separators.

        Walks the layout tree, reads each leaf's VTE column/row count,
        and sums them with +1 per separator (matching tmux's layout math).
        Detects unallocated widgets (1x1 pixel) and falls back to tmux
        node dimensions to avoid stale set_size() column counts.
        """
        if node.is_leaf:
            terminal = self.pane_to_terminal.get(node.pane_id)
            if terminal:
                try:
                    vte_alloc = terminal.vte.get_allocation()
                    char_w = terminal.vte.get_char_width()
                    char_h = terminal.vte.get_char_height()
                    if (char_w > 0 and char_h > 0
                            and vte_alloc.width > char_w
                            and vte_alloc.height > char_h):
                        return (terminal.vte.get_column_count(),
                                terminal.vte.get_row_count())
                except Exception:
                    pass
            return node.width, node.height

        child_sizes = [self._sum_vte_sizes(c) for c in node.children]
        n_seps = len(child_sizes) - 1

        if node.orientation == 'h':
            total_cols = sum(s[0] for s in child_sizes) + n_seps
            max_rows = max((s[1] for s in child_sizes), default=0)
            return total_cols, max_rows
        else:
            max_cols = max((s[0] for s in child_sizes), default=0)
            total_rows = sum(s[1] for s in child_sizes) + n_seps
            return max_cols, total_rows

    def get_initial_layout(self):
        """Build Terminator layout from tmux's current state.

        Called during startup to configure the initial window layout.
        Returns the layout dict or None if not yet available.
        """
        if not self.window_layouts:
            return None

        from terminatorlib.tmux.layout import parse_tmux_layout, build_terminator_layout
        nodes = []
        total_cols = 0
        total_rows = 0
        for window_id, layout_string in self.window_layouts.items():
            try:
                node = parse_tmux_layout(layout_string)
            except ValueError as e:
                dbg('TmuxController: skipping bad layout for %s: %s' % (window_id, e))
                continue
            nodes.append(node)
            total_cols = max(total_cols, node.width)
            total_rows = max(total_rows, node.height)

        return build_terminator_layout(nodes, total_cols, total_rows)
