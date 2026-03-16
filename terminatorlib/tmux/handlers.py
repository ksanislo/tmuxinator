"""Tmux notification handlers - maps tmux events to Terminator widget operations.

All GTK operations are dispatched via GLib.idle_add() for thread safety.
"""

from gi.repository import GLib

from terminatorlib.util import dbg
from terminatorlib.tmux.layout import (
    parse_tmux_layout, get_pane_ids, find_pane_parent, find_pane_node,
)


ALTERNATE_SCREEN_ENTER = b'\x1b[?1049h'
ALTERNATE_SCREEN_EXIT = b'\x1b[?1049l'


class TmuxHandlers:
    """Handles tmux notifications and maps them to Terminator operations."""

    def __init__(self, controller):
        self.controller = controller
        self.protocol = controller.protocol
        self._layout_trees = {}  # window_id -> LayoutNode tree

        # Register handlers
        self.protocol.add_handler('output', self.on_output)
        self.protocol.add_handler('layout-change', self.on_layout_change)
        self.protocol.add_handler('window-add', self.on_window_add)
        self.protocol.add_handler('window-close', self.on_window_close)
        self.protocol.add_handler('unlinked-window-close', self.on_window_close)
        self.protocol.add_handler('window-renamed', self.on_window_renamed)
        self.protocol.add_handler('exit', self.on_exit)

    def on_output(self, info):
        """Handle %output: feed data to the terminal's VTE."""
        pane_id = info['pane_id']
        data = info['data']

        terminal = self.controller.pane_to_terminal.get(pane_id)
        if not terminal:
            return

        # Track alternate screen state (vim, less, etc.)
        if ALTERNATE_SCREEN_ENTER in data:
            self.controller.pane_alternate[pane_id] = True
        if ALTERNATE_SCREEN_EXIT in data:
            self.controller.pane_alternate[pane_id] = False

        # Feed to VTE on GTK thread
        GLib.idle_add(self._feed_terminal, terminal, data)

    def _feed_terminal(self, terminal, data):
        """Feed data to terminal VTE widget. Must be called on GTK thread."""
        try:
            if hasattr(terminal, 'vte') and terminal.vte:
                terminal.vte.feed(data)
        except Exception as e:
            dbg('TmuxHandlers: feed error: %s' % e)
        return False

    def _find_tmux_window(self, terminator):
        """Find the Terminator window that contains tmux terminals."""
        for window in terminator.windows:
            for terminal in window.get_terminals():
                if terminal.tmux_pane_id is not None:
                    return window
        return None

    def on_layout_change(self, info):
        """Handle %layout-change: sync Terminator splits with tmux layout."""
        window_id = info['window_id']
        layout_string = info['layout_string']

        try:
            new_tree = parse_tmux_layout(layout_string)
        except ValueError as e:
            dbg('TmuxHandlers: layout parse error: %s' % e)
            return

        new_panes = get_pane_ids(new_tree)
        old_tree = self._layout_trees.get(window_id)
        old_panes = get_pane_ids(old_tree) if old_tree else set()

        deleted_panes = old_panes - new_panes
        added_panes = new_panes - old_panes

        self._layout_trees[window_id] = new_tree
        self.controller.window_layouts[window_id] = layout_string

        if deleted_panes:
            GLib.idle_add(self._close_panes, deleted_panes)
        elif added_panes:
            GLib.idle_add(self._add_panes, added_panes, new_tree)

    def _close_panes(self, pane_ids):
        """Close terminals for deleted panes. Called on GTK thread."""
        for pane_id in pane_ids:
            terminal = self.controller.pane_to_terminal.get(pane_id)
            if terminal:
                terminal._tmux_closing = True
                terminal.close()
        return False

    def _add_panes(self, pane_ids, layout_tree):
        """Create terminals for new panes. Called on GTK thread."""
        from terminatorlib.factory import Factory
        maker = Factory()

        for pane_id in pane_ids:
            pane_node = find_pane_node(pane_id, layout_tree)
            parent_container = find_pane_parent(pane_id, layout_tree)
            if not pane_node or not parent_container:
                continue

            # Find the sibling pane (the one before the new pane in the parent)
            idx = None
            for i, child in enumerate(parent_container.children):
                if child.is_leaf and child.pane_id == pane_id:
                    idx = i
                    break
            if idx is None:
                continue

            # Find the previous sibling's terminal
            sibling_idx = idx - 1 if idx > 0 else idx + 1
            if sibling_idx >= len(parent_container.children):
                continue
            sibling = parent_container.children[sibling_idx]
            if not sibling.is_leaf:
                continue
            old_terminal = self.controller.pane_to_terminal.get(sibling.pane_id)
            if not old_terminal:
                continue

            # Create new terminal
            new_terminal = maker.make('Terminal')
            new_terminal.tmux_pane_id = pane_id
            self.controller.register_terminal(pane_id, new_terminal)

            # Capture initial content
            self.protocol.send_command(
                'capture-pane -J -p -t {} -eC -S - -E -'.format(pane_id),
                callback=lambda result, t=new_terminal: self._feed_captured(t, result),
            )

            # Split the existing terminal
            old_parent = old_terminal.get_parent()
            vertical = parent_container.orientation == 'v'
            widget_first = idx > sibling_idx
            old_parent.split_axis(old_terminal, vertical=vertical,
                                   sibling=new_terminal, widgetfirst=widget_first)

        return False

    def _feed_captured(self, terminal, result):
        """Feed captured pane content to a terminal."""
        if result.is_error or not result.output_lines:
            return
        from terminatorlib.tmux.protocol import unescape_tmux_output
        raw = b'\r\n'.join(line for line in result.output_lines if line)
        data = unescape_tmux_output(raw)
        GLib.idle_add(self._feed_terminal, terminal, data)

    def on_window_add(self, info):
        """Handle %window-add: query the new window's layout, then create a tab."""
        window_id = info.get('window_id', '')
        dbg('TmuxHandlers: window-add: %s' % window_id)
        # Query the layout of this specific window
        self.protocol.send_command(
            'list-windows -F "W:#{{window_id}}:#{{window_layout}}" -f "#{{==:#{{window_id}},{wid}}}"'.format(
                wid=window_id),
            callback=lambda result, wid=window_id: self._on_new_window_layout(wid, result),
        )

    def _on_new_window_layout(self, window_id, result):
        """Handle layout query for a newly added window."""
        if result.is_error:
            dbg('TmuxHandlers: new window layout query error')
            return
        for line in result.output_lines:
            decoded = line.decode('utf-8', errors='replace').strip()
            if not decoded.startswith('W:@'):
                continue
            rest = decoded[2:]
            parts = rest.split(':', 1)
            if len(parts) < 2:
                continue
            wid = parts[0]
            layout_string = parts[1]
            self.controller.window_layouts[window_id] = layout_string
            try:
                tree = parse_tmux_layout(layout_string)
                self._layout_trees[window_id] = tree
            except ValueError as e:
                dbg('TmuxHandlers: parse error for new window %s: %s' % (window_id, e))
                return
            GLib.idle_add(self._create_tab_for_window, window_id, tree)
            return

    def _create_tab_for_window(self, window_id, tree):
        """Create a new Terminator tab for a tmux window. Called on GTK thread."""
        from terminatorlib.factory import Factory
        from terminatorlib.terminator import Terminator
        from terminatorlib.tmux.layout import layout_to_terminator

        term = Terminator()
        maker = Factory()

        # Build flat layout for this single window's panes
        flat_layout, _, _ = layout_to_terminator(tree, 'tab_root')

        # For a single pane, just make a terminal
        pane_ids = get_pane_ids(tree)
        if len(pane_ids) == 1:
            pane_id = list(pane_ids)[0]
            new_terminal = maker.make('Terminal')
            new_terminal.tmux_pane_id = pane_id
            self.controller.register_terminal(pane_id, new_terminal)
        else:
            # For multiple panes, create the first terminal and we'll
            # let layout-change handle the splits
            pane_id = list(pane_ids)[0]
            new_terminal = maker.make('Terminal')
            new_terminal.tmux_pane_id = pane_id
            self.controller.register_terminal(pane_id, new_terminal)

        # Find the window that contains tmux terminals
        window = self._find_tmux_window(term)
        if window:
            # Ensure window has a notebook
            from terminatorlib.factory import Factory as F
            if not window.is_child_notebook():
                F().make('Notebook', window=window)
            window.get_child().newtab(widget=new_terminal)
        else:
            dbg('TmuxHandlers: no tmux window to add tab to')
            return False

        # Capture initial content for panes in this window
        for pid in pane_ids:
            self.protocol.send_command(
                'capture-pane -J -p -t {} -eC -S - -E -'.format(pid),
                callback=lambda result, p=pid: self._feed_initial_capture(p, result),
            )
        return False

    def on_window_close(self, info):
        """Handle %window-close: close all terminals in that window."""
        window_id = info.get('window_id', '')
        dbg('TmuxHandlers: window-close: %s (known trees: %s)' % (
            window_id, list(self._layout_trees.keys())))
        tree = self._layout_trees.pop(window_id, None)
        self.controller.window_layouts.pop(window_id, None)
        if tree:
            pane_ids = get_pane_ids(tree)
            dbg('TmuxHandlers: closing panes: %s' % pane_ids)
            GLib.idle_add(self._close_panes, pane_ids)
        else:
            dbg('TmuxHandlers: no tree found for window %s' % window_id)

    def on_window_renamed(self, info):
        """Handle %window-renamed: update tab title."""
        window_id = info.get('window_id', '')
        name = info.get('name', '')
        dbg('TmuxHandlers: window-renamed: %s -> %s' % (window_id, name))

    def on_exit(self, info):
        """Handle %exit: clean up everything."""
        reason = info.get('reason', 'unknown')
        dbg('TmuxHandlers: exit: %s' % reason)
        GLib.idle_add(self._handle_exit)

    def _handle_exit(self):
        """Handle tmux exit on GTK thread."""
        self.controller.stop()
        return False

    def on_initial_list_windows(self, result):
        """Handle the initial list-windows response.

        Parses window layouts and stores them for initial layout building.
        """
        if result.is_error:
            dbg('TmuxHandlers: initial list-windows error')
            return

        for line in result.output_lines:
            decoded = line.decode('utf-8', errors='replace').strip()
            if not decoded:
                continue
            # Format: W:@WINDOW_ID:LAYOUT_STRING
            if not decoded.startswith('W:@'):
                dbg('TmuxHandlers: skipping invalid line: %s' % decoded)
                continue
            # Strip the W: prefix, split on first : after window_id
            rest = decoded[2:]  # "@WINDOW_ID:LAYOUT_STRING"
            parts = rest.split(':', 1)
            if len(parts) < 2:
                continue
            window_id = parts[0]
            layout_string = parts[1]
            self.controller.window_layouts[window_id] = layout_string
            try:
                tree = parse_tmux_layout(layout_string)
                self._layout_trees[window_id] = tree
            except ValueError as e:
                dbg('TmuxHandlers: parse error for window %s: %s' % (window_id, e))

        dbg('TmuxHandlers: initial layout parsed, %d windows' %
            len(self.controller.window_layouts))

        # Signal the controller that the initial layout is ready
        self.controller._initial_layout_ready.set()

    def capture_initial_content(self):
        """Capture and display initial pane content after terminals are registered."""
        # Send initial client size based on actual terminal dimensions
        self._send_initial_resize()
        for window_id, tree in self._layout_trees.items():
            for pane_id in get_pane_ids(tree):
                self.protocol.send_command(
                    'capture-pane -J -p -t {} -eC -S - -E -'.format(pane_id),
                    callback=lambda result, pid=pane_id: self._feed_initial_capture(pid, result),
                )

    def _send_initial_resize(self):
        """Send refresh-client with actual terminal dimensions."""
        max_cols = 0
        max_rows = 0
        for terminal in self.controller.pane_to_terminal.values():
            try:
                c = terminal.vte.get_column_count()
                r = terminal.vte.get_row_count()
                max_cols = max(max_cols, c)
                max_rows = max(max_rows, r)
            except Exception:
                pass
        if max_cols > 0 and max_rows > 0:
            dbg('TmuxHandlers: initial resize to %dx%d' % (max_cols, max_rows))
            self.protocol.send_command(
                'refresh-client -C {},{}'.format(max_cols, max_rows))

    def _feed_initial_capture(self, pane_id, result):
        """Feed initially captured content to the right terminal."""
        if result.is_error:
            return
        terminal = self.controller.pane_to_terminal.get(pane_id)
        if terminal and result.output_lines:
            from terminatorlib.tmux.protocol import unescape_tmux_output
            raw = b'\r\n'.join(line for line in result.output_lines if line)
            data = unescape_tmux_output(raw)
            GLib.idle_add(self._feed_terminal, terminal, data)

