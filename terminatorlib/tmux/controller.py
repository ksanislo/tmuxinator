"""Tmux controller - maps tmux state to Terminator widgets.

Singleton (Borg pattern) that manages the tmux protocol connection,
terminal registration, key translation, and resize handling.
"""

import threading

from gi.repository import Gdk, GLib

from terminatorlib.borg import Borg
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


class TmuxController(Borg):
    """Singleton controller bridging tmux and Terminator."""

    active = None
    session_name = None
    protocol = None
    handlers = None
    pane_to_terminal = None
    terminal_to_pane = None
    pane_alternate = None
    window_layouts = None
    _resize_timer = None
    _initial_layout_ready = None

    def __init__(self):
        Borg.__init__(self, self.__class__.__name__)
        self.prepare_attributes()

    def prepare_attributes(self):
        if self.pane_to_terminal is None:
            self.active = False
            self.pane_to_terminal = {}
            self.terminal_to_pane = {}
            self.pane_alternate = {}
            self.window_layouts = {}
            self._resize_timer = None
            self._initial_layout_ready = threading.Event()

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
        dbg('TmuxController: started from PTY fd %d' % pty_fd)

        # Query initial state and wait for it
        self._query_initial_state()
        dbg('TmuxController: waiting for initial layout...')
        self._initial_layout_ready.wait(timeout=5.0)
        if not self._initial_layout_ready.is_set():
            dbg('TmuxController: timeout waiting for initial layout')
        else:
            dbg('TmuxController: initial layout ready')

    def stop(self):
        """Detach from tmux and clean up."""
        if self.protocol:
            self.protocol.stop()
        self.active = False
        self.pane_to_terminal.clear()
        self.terminal_to_pane.clear()
        self.pane_alternate.clear()
        self.window_layouts.clear()
        dbg('TmuxController: stopped')

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
        """Query tmux for window layouts.
        Uses colon separator to avoid brace-related PTY echo issues.
        """
        self.protocol.send_command(
            'list-windows -F "W:#{window_id}:#{window_layout}"',
            callback=self.handlers.on_initial_list_windows,
        )

    def register_terminal(self, pane_id, terminal):
        """Register a terminal widget for a tmux pane."""
        self.pane_to_terminal[pane_id] = terminal
        self.terminal_to_pane[terminal] = pane_id
        dbg('TmuxController: registered terminal for pane %s' % pane_id)

    def unregister_terminal(self, terminal):
        """Unregister a terminal widget."""
        pane_id = self.terminal_to_pane.pop(terminal, None)
        if pane_id:
            self.pane_to_terminal.pop(pane_id, None)
            self.pane_alternate.pop(pane_id, None)
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
        """Notify tmux of terminal resize. Debounced to 100ms.

        Sends both the overall client size and individual pane resize.
        """
        # Cancel any pending resize
        if self._resize_timer:
            GLib.source_remove(self._resize_timer)

        def do_resize():
            self._resize_timer = None
            # Calculate total client size from all registered terminals
            max_cols = 0
            max_rows = 0
            for t in self.terminal_to_pane:
                try:
                    c = t.vte.get_column_count()
                    r = t.vte.get_row_count()
                    max_cols = max(max_cols, c)
                    max_rows = max(max_rows, r)
                except Exception:
                    pass
            if max_cols > 0 and max_rows > 0:
                self.protocol.send_command(
                    'refresh-client -C {},{}'.format(max_cols, max_rows))
            # Resize the individual pane
            pane_id = self.terminal_to_pane.get(terminal)
            if pane_id:
                self.protocol.send_command(
                    'resize-pane -t {} -x {} -y {}'.format(pane_id, cols, rows))
            return False

        self._resize_timer = GLib.timeout_add(100, do_resize)

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
