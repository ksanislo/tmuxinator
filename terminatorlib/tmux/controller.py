"""Tmux controller - maps tmux state to Terminator widgets.

Singleton (Borg pattern) that manages the tmux protocol connection,
terminal registration, key translation, and resize handling.
"""

import threading

from gi.repository import Gdk, GLib

from terminatorlib.borg import Borg
from terminatorlib.util import dbg
from terminatorlib.tmux import tmux_dbg
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
    _last_client_size = None
    _last_pane_sizes = None
    _last_window_pixels = None
    _applying_layout = None
    _layout_applied_time = None
    _prev_vte_sizes = None

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
            self._last_pane_sizes = {}
            self._prev_vte_sizes = {}
            self._applying_layout = False
            self._layout_applied_time = 0
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

        Distinguishes between window resize (sends refresh-client -C)
        and split bar drag (sends relative resize-pane commands).
        """
        pane_id = self.terminal_to_pane.get(terminal, '?')
        tmux_dbg('notify_resize: %s %dx%d applying=%s' % (
            pane_id, cols, rows, self._applying_layout))
        # Don't send resize while we're applying a layout from tmux
        if self._applying_layout:
            tmux_dbg('notify_resize: suppressed (applying_layout)')
            return
        # Cancel any pending resize
        if self._resize_timer:
            GLib.source_remove(self._resize_timer)

        def do_resize():
            self._resize_timer = None

            # Always snapshot current VTE sizes first, so _prev_vte_sizes
            # stays current even when we suppress sending commands
            def _snapshot_vte_sizes():
                for t, pane_id in self.terminal_to_pane.items():
                    try:
                        self._prev_vte_sizes[pane_id] = (
                            t.vte.get_column_count(), t.vte.get_row_count())
                    except Exception:
                        pass

            # Skip sending commands if a layout was just applied or we just
            # sent a resize command (suppress echo-back from tmux response)
            import time as _time
            elapsed = _time.monotonic() - self._layout_applied_time
            if elapsed < 0.3:
                tmux_dbg('notify_resize: suppressed (echo-back %.3fs ago)' % elapsed)
                _snapshot_vte_sizes()
                return False

            # Detect if the overall window changed size (vs just a split drag)
            window_resized = False
            for t in self.terminal_to_pane:
                try:
                    top = t.get_toplevel()
                    alloc = top.get_allocation()
                    px = (alloc.width, alloc.height)
                    if px != self._last_window_pixels:
                        if self._last_window_pixels is not None:
                            tmux_dbg('window pixels changed %s -> %s' % (self._last_window_pixels, px))
                            window_resized = True
                        self._last_window_pixels = px
                    break
                except Exception:
                    pass

            tmux_dbg('notify_resize: window_resized=%s pane_count=%d' % (
                window_resized, len(self.terminal_to_pane)))
            if window_resized or len(self.terminal_to_pane) <= 1:
                # Window resize: send refresh-client -C with total size
                total_cols, total_rows = self._calculate_client_size()
                if total_cols > 0 and total_rows > 0:
                    size = (total_cols, total_rows)
                    if size != self._last_client_size:
                        self._last_client_size = size
                        tmux_dbg('sending refresh-client -C %d,%d' % (total_cols, total_rows))
                        self.protocol.send_command(
                            'refresh-client -C {},{}'.format(total_cols, total_rows))
                        # Refresh layout state after resize
                        self._refresh_layout_state()
            else:
                # Split bar drag: send absolute resize for the most-changed pane
                self._send_split_bar_resize()

            _snapshot_vte_sizes()
            return False

        self._resize_timer = GLib.timeout_add(100, do_resize)

    def _send_split_bar_resize(self):
        """Send absolute resize-pane for the pane with the largest size change.

        Uses absolute -x/-y for a single pane — tmux adjusts neighbors
        automatically. This avoids needing to figure out which direction
        the divider moved (which would require layout tree position info).
        """
        best_pane = None
        best_delta = 0
        best_cols = 0
        best_rows = 0
        best_dcols = 0
        best_drows = 0

        for terminal, pane_id in self.terminal_to_pane.items():
            try:
                cur_cols = terminal.vte.get_column_count()
                cur_rows = terminal.vte.get_row_count()
            except Exception:
                continue

            prev = self._prev_vte_sizes.get(pane_id)
            if prev is None:
                continue

            prev_cols, prev_rows = prev
            dcols = abs(cur_cols - prev_cols)
            drows = abs(cur_rows - prev_rows)
            delta = dcols + drows

            if delta > best_delta:
                best_delta = delta
                best_pane = pane_id
                best_cols = cur_cols
                best_rows = cur_rows
                best_dcols = dcols
                best_drows = drows

        # Log all pane deltas for debugging
        for terminal, pane_id in self.terminal_to_pane.items():
            try:
                cur = (terminal.vte.get_column_count(), terminal.vte.get_row_count())
                prev = self._prev_vte_sizes.get(pane_id)
                if prev and cur != prev:
                    tmux_dbg('split drag delta: %s prev=%dx%d cur=%dx%d' % (
                        pane_id, prev[0], prev[1], cur[0], cur[1]))
            except Exception:
                pass

        if best_pane and best_delta > 0:
            import time as _time
            # Only send the dimension(s) that actually changed
            parts = ['resize-pane -t {}'.format(best_pane)]
            if best_dcols > 0:
                parts.append('-x {}'.format(best_cols))
            if best_drows > 0:
                parts.append('-y {}'.format(best_rows))
            cmd = ' '.join(parts)
            tmux_dbg('split drag: %s' % cmd)
            self.protocol.send_command(cmd)
            # Suppress echo-back from the layout-change response
            self._layout_applied_time = _time.monotonic()
            self._refresh_layout_state()
        else:
            tmux_dbg('split drag: no pane changed (best_delta=0)')

    def _refresh_layout_state(self):
        """Send list-windows to refresh our layout tree after a resize."""
        self.protocol.send_command(
            'list-windows -F "W:#{window_id}:#{window_layout}"',
            callback=self.handlers.on_initial_list_windows,
        )

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

        tmux_dbg('DEBUG %s: cell=%dx%d vte_px=%dx%d vte_chars=%dx%d '
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
                    tmux_dbg('client size: %dx%d (from VTE grid + separators)' % (
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
