"""Tmux notification handlers - maps tmux events to Terminator widget operations.

All GTK operations are dispatched via GLib.idle_add() for thread safety.
"""

from gi.repository import GLib

from terminatorlib.util import dbg
from terminatorlib.tmux import tmux_dbg
from terminatorlib.tmux.layout import (
    parse_tmux_layout, get_pane_ids, find_pane_parent, find_pane_node,
)


ALTERNATE_SCREEN_ENTER = b'\x1b[?1049h'
ALTERNATE_SCREEN_EXIT = b'\x1b[?1049l'

SHELL_COMMANDS = {'bash', 'zsh', 'fish', 'sh', 'dash', 'csh', 'tcsh', 'ksh'}


class TmuxHandlers:
    """Handles tmux notifications and maps them to Terminator operations."""

    def __init__(self, controller):
        self.controller = controller
        self.protocol = controller.protocol
        self._layout_trees = {}  # window_id -> LayoutNode tree
        self._needs_ratio_retry = False
        self._reconcile_timer = None
        self._capture_after_ratios = False
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
        """Find the Terminator window that contains this controller's terminals."""
        for window in terminator.windows:
            for terminal in window.get_terminals():
                if terminal in self.controller.terminal_to_pane:
                    return window
        return None

    def on_layout_change(self, info):
        """Handle %layout-change: sync Terminator splits with tmux layout."""
        import time, traceback
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

        # Debug: log every layout-change with dimensions and what triggered it
        old_dims = '%dx%d' % (old_tree.width, old_tree.height) if old_tree else 'none'
        new_dims = '%dx%d' % (new_tree.width, new_tree.height)
        client_size = self.controller._last_client_size
        elapsed = time.monotonic() - self.controller._layout_applied_time \
            if self.controller._layout_applied_time else float('inf')
        tmux_dbg('layout-change: %s old=%s new=%s client=%s '
                 'del=%s add=%s elapsed=%.3fs applying=%s' % (
                     window_id, old_dims, new_dims, client_size,
                     deleted_panes or '{}', added_panes or '{}',
                     elapsed, self.controller._applying_layout))

        self._layout_trees[window_id] = new_tree
        self.controller.window_layouts[window_id] = layout_string

        if deleted_panes:
            GLib.idle_add(self._close_panes, deleted_panes)
        elif added_panes:
            GLib.idle_add(self._add_panes, added_panes, new_tree)
        else:
            # If the layout dimensions changed and we didn't cause it
            # (unsolicited = another client resized), resize our window
            # to match — tmux is the authority for external changes.
            new_size = (new_tree.width, new_tree.height)
            we_caused_it = elapsed < 1.0
            if client_size and new_size != client_size and not we_caused_it:
                tmux_dbg('layout-change: unsolicited size change '
                         '%dx%d -> %dx%d (elapsed=%.3fs), resizing window' % (
                             client_size[0], client_size[1],
                             new_size[0], new_size[1], elapsed))
                self.controller._last_client_size = new_size
                GLib.idle_add(self._resize_window_to_tree, new_tree)
            elif client_size and new_size != client_size:
                tmux_dbg('layout-change: echo-back size change '
                         '%dx%d -> %dx%d (elapsed=%.3fs), not resizing' % (
                             client_size[0], client_size[1],
                             new_size[0], new_size[1], elapsed))

            # Same panes but resized — update our splits to match
            GLib.idle_add(self._update_pane_sizes, new_tree)

    def _log_layout_sizes(self, node, depth=0):
        """Log tmux layout sizes vs actual VTE and Terminal widget sizes."""
        if node.is_leaf:
            terminal = self.controller.pane_to_terminal.get(node.pane_id)
            if terminal:
                try:
                    vte_cols = terminal.vte.get_column_count()
                    vte_rows = terminal.vte.get_row_count()
                    tw_cols, tw_rows = self.controller._pane_size_for_tmux(terminal)
                    match = 'OK' if (tw_cols == node.width and tw_rows == node.height) else 'MISMATCH'
                    tmux_dbg('  %s%s: tmux=%dx%d widget=%dx%d vte=%dx%d %s' % (
                        '  ' * depth, node.pane_id, node.width, node.height,
                        tw_cols, tw_rows, vte_cols, vte_rows, match))
                except Exception:
                    pass
        else:
            tmux_dbg('  %s%s split %dx%d:' % ('  ' * depth, node.orientation, node.width, node.height))
            for child in node.children:
                self._log_layout_sizes(child, depth + 1)

    def _update_pane_sizes(self, tree):
        """Update split ratios to match tmux's pane dimensions.
        Called on GTK thread."""
        import time
        self.controller._applying_layout = True
        deferred = False
        try:
            self._record_tmux_sizes(tree)
            if not tree.is_leaf:
                self._needs_ratio_retry = False
                self._apply_ratios(tree)
                if self._needs_ratio_retry:
                    tmux_dbg('retrying ratios in 100ms (unallocated paneds)')
                    GLib.timeout_add(100, self._apply_ratios_and_finish, tree)
                    deferred = True
        finally:
            if not deferred:
                self.controller._applying_layout = False
                self.controller._layout_applied_time = time.monotonic()
                # Snapshot VTE sizes so notify_resize won't see the
                # ratio-driven size change as a "split drag" — but
                # skip during initial startup (client_size not yet set)
                # so the first do_resize can detect the delta and send
                # resize-pane, which triggers fresh content from tmux.
                if self.controller._last_client_size is not None:
                    self._snapshot_vte_sizes()
                self._schedule_reconcile(tree)
                if self._capture_after_ratios:
                    self._capture_after_ratios = False
                    self._send_captures()
        return False

    def _apply_ratios_and_finish(self, tree):
        """Deferred callback to apply ratios for unallocated Paneds.

        _applying_layout stays True until all ratios are applied,
        providing continuous suppression of resize echo-back.
        """
        import time
        self._needs_ratio_retry = False
        try:
            self._apply_ratios(tree)
            if self._needs_ratio_retry:
                tmux_dbg('retrying ratios in 100ms (unallocated paneds)')
                GLib.timeout_add(100, self._apply_ratios_and_finish, tree)
                return False  # keep _applying_layout True
        except Exception:
            pass
        self.controller._applying_layout = False
        self.controller._layout_applied_time = time.monotonic()
        if self.controller._last_client_size is not None:
            self._snapshot_vte_sizes()
        self._schedule_reconcile(tree)
        return False

    def _snapshot_vte_sizes(self):
        """Snapshot current VTE sizes into _prev_vte_sizes.

        Called after layout application so notify_resize won't
        misinterpret ratio-driven VTE changes as split-bar drags.
        """
        for t, pid in self.controller.terminal_to_pane.items():
            try:
                self.controller._prev_vte_sizes[pid] = (
                    t.vte.get_column_count(), t.vte.get_row_count())
            except Exception:
                pass

    def _schedule_reconcile(self, tree):
        """Schedule a deferred reconciliation of pane sizes with tmux.

        After ratio application, GTK's pixel rounding may cause VTE sizes
        to differ from what tmux expects. This sends resize-pane commands
        to sync tmux with the actual VTE sizes. Runs after the 0.3s
        suppression window so notify_resize won't interfere.
        """
        if self._reconcile_timer:
            GLib.source_remove(self._reconcile_timer)
        self._reconcile_timer = GLib.timeout_add(
            500, self._reconcile_pane_sizes, tree)

    def _reconcile_pane_sizes(self, tree):
        """Send resize-pane for any pane where VTE size differs from tmux."""
        import time
        self._reconcile_timer = None
        client_size = self.controller._last_client_size
        tmux_dbg('reconcile: tree=%dx%d client=%s' % (
            tree.width, tree.height, client_size))
        if client_size and (tree.width > client_size[0] or tree.height > client_size[1]):
            tmux_dbg('reconcile: skipping — layout %dx%d exceeds client %dx%d' % (
                tree.width, tree.height, client_size[0], client_size[1]))
            return False

        mismatches = []
        self._collect_mismatches(tree, mismatches)
        if not mismatches:
            tmux_dbg('reconcile: all panes match tmux')
            return False
        tmux_dbg('reconcile: fixing %d mismatched pane(s):' % len(mismatches))
        for pane_id, vte_cols, vte_rows, tmux_cols, tmux_rows in mismatches:
            tmux_dbg('  %s: vte=%dx%d tmux=%dx%d' % (
                pane_id, vte_cols, vte_rows, tmux_cols, tmux_rows))
            parts = ['resize-pane -t {}'.format(pane_id)]
            if vte_cols != tmux_cols:
                parts.append('-x {}'.format(vte_cols))
            if vte_rows != tmux_rows:
                parts.append('-y {}'.format(vte_rows))
            cmd = ' '.join(parts)
            tmux_dbg('reconcile: %s' % cmd)
            self.protocol.send_command(cmd)
        # Suppress echo-back from layout-change responses
        self.controller._layout_applied_time = time.monotonic()
        # Snapshot VTE sizes so subsequent notify_resize has correct baseline
        for t, pid in self.controller.terminal_to_pane.items():
            try:
                self.controller._prev_vte_sizes[pid] = (
                    t.vte.get_column_count(), t.vte.get_row_count())
            except Exception:
                pass
        self.controller._refresh_layout_state()
        return False

    def _collect_mismatches(self, node, out):
        """Collect panes where VTE size differs from tmux's expected size."""
        if node.is_leaf:
            terminal = self.controller.pane_to_terminal.get(node.pane_id)
            if terminal:
                try:
                    vte_cols = terminal.vte.get_column_count()
                    vte_rows = terminal.vte.get_row_count()
                    if vte_cols != node.width or vte_rows != node.height:
                        out.append((node.pane_id, vte_cols, vte_rows,
                                    node.width, node.height))
                except Exception:
                    pass
        else:
            for child in node.children:
                self._collect_mismatches(child, out)

    def _record_tmux_sizes(self, node):
        """Record tmux's reported pane sizes to prevent resize feedback loops.

        Only updates _last_pane_sizes (tmux's view). Does NOT touch
        _prev_vte_sizes — those track actual VTE widget sizes and are
        only updated from real VTE measurements in do_resize.
        """
        if node.is_leaf:
            self.controller._last_pane_sizes[node.pane_id] = (node.width, node.height)
        else:
            for child in node.children:
                self._record_tmux_sizes(child)

    def _apply_ratios(self, node):
        """Recursively set split ratios on Paned containers to match tmux layout.

        Computes ratios in pixels (not characters) to account for the
        scrollbar width and titlebar height inside each Terminal widget.
        Without this correction, each pane loses ~1 column/row because
        the character-based ratio doesn't reserve space for scrollbars.

        For N-ary splits (3+ children), the GTK widget tree uses nested
        binary paneds: [A, B, C] becomes Paned1(A, Paned2(B, C)).
        This method sets ratios on all intermediate paneds, not just the
        outermost one.
        """
        if node.is_leaf or len(node.children) < 2:
            return

        # Set ratios for each binary split in the chain.
        # For children [c0, c1, c2, ...], we have paneds:
        #   Paned_0: c0 vs (c1 + c2 + ...)
        #   Paned_1: c1 vs (c2 + ...)     (intermediate paned)
        #   etc.
        orient = node.orientation  # 'h' or 'v'
        remaining = node.children

        while len(remaining) >= 2:
            first_leaf_l = self._first_leaf(remaining[0])
            first_leaf_r = self._first_leaf(remaining[1])
            term_l = self.controller.pane_to_terminal.get(first_leaf_l.pane_id)
            term_r = self.controller.pane_to_terminal.get(first_leaf_r.pane_id)

            if not (term_l and term_r):
                break

            paned = self._find_common_paned(term_l, term_r)
            if not (paned and hasattr(paned, 'ratio')):
                break

            # Get metrics from an allocated terminal
            char_w, char_h, sb_w, tb_h, vpad_x, vpad_y = \
                self._get_terminal_metrics(term_l)
            if char_w <= 0 or char_h <= 0:
                char_w, char_h, sb_w, tb_h, vpad_x, vpad_y = \
                    self._get_terminal_metrics(term_r)
            if char_w <= 0 or char_h <= 0:
                break

            handle_size = paned.get_handlesize()
            paned_len = paned.get_length()

            if paned_len <= handle_size:
                tmux_dbg('ratio SKIPPED: paned not allocated '
                         '(len=%d <= handle=%d)' % (paned_len, handle_size))
                self._needs_ratio_retry = True
                break

            left_px = self._subtree_px(
                remaining[0], orient,
                char_w, char_h, sb_w, tb_h,
                handle_size, vpad_x, vpad_y)
            right_px = sum(
                self._subtree_px(c, orient,
                                 char_w, char_h, sb_w, tb_h,
                                 handle_size, vpad_x, vpad_y)
                for c in remaining[1:])
            # Add separators between right-side children
            if len(remaining) > 2:
                sep = char_w if orient == 'h' else char_h
                right_px += (len(remaining) - 2) * sep

            # Pad the first child so the visual gap
            # (padding + handle) = 1 character cell.
            char_sep = char_w if orient == 'h' else char_h
            if char_sep > handle_size:
                left_px += char_sep - handle_size

            total_px = left_px + right_px
            if total_px > 0:
                ratio = left_px / total_px
                tmux_dbg('ratio %s-split: left=%dpx right=%dpx '
                         'ratio=%.4f old=%.4f paned=%d '
                         'char=%dx%d sb=%d tb=%d handle=%d '
                         'vte_pad=%dx%d' % (
                             orient, left_px, right_px, ratio,
                             paned.ratio, paned_len,
                             char_w, char_h, sb_w, tb_h,
                             handle_size, vpad_x, vpad_y))
                if abs(paned.ratio - ratio) > 0.005:
                    paned.ratio = ratio
                    paned.set_position_by_ratio()

            # Move to the next intermediate paned
            remaining = remaining[1:]

        # Recurse into children (handle nested splits within each child)
        for child in node.children:
            if not child.is_leaf:
                self._apply_ratios(child)

    def _subtree_px(self, node, orientation, char_w, char_h, sb_w, tb_h,
                     handle_size, vte_pad_x=0, vte_pad_y=0):
        """Compute target pixel extent of a layout subtree along orientation.

        For 'h' orientation: returns width in pixels (chars*char_w + vte_pad + scrollbar + handles).
        For 'v' orientation: returns height in pixels (chars*char_h + vte_pad + titlebar + handles).

        vte_pad_x/y accounts for VTE's internal CSS padding (typically 1px
        each side). Without this, VTE gets exactly cols*char_w pixels but
        subtracts its padding first, leaving room for only cols-1 characters.
        """
        if node.is_leaf:
            if orientation == 'h':
                return node.width * char_w + vte_pad_x + sb_w
            else:
                return node.height * char_h + vte_pad_y + tb_h

        if node.orientation == orientation:
            # Same direction: sum children + separators.
            # For v-splits, use char_h as separator size (not handle_size)
            # so the total matches tmux's 1-char-tall separators. The
            # extra pixels (char_h - handle_size) become padding in
            # _apply_ratios to visually merge with the handle.
            child_px = [self._subtree_px(c, orientation,
                                         char_w, char_h, sb_w, tb_h,
                                         handle_size, vte_pad_x, vte_pad_y)
                        for c in node.children]
            sep = char_w if orientation == 'h' else char_h
            return sum(child_px) + (len(child_px) - 1) * sep
        else:
            # Cross direction: take max — a v-split child needs more
            # pixels than a leaf for the same character count (extra
            # titlebars, VTE padding, handles inside the subtree).
            return max(self._subtree_px(c, orientation,
                                        char_w, char_h, sb_w, tb_h,
                                        handle_size, vte_pad_x, vte_pad_y)
                       for c in node.children)

    def _get_terminal_metrics(self, terminal):
        """Get char/scrollbar/titlebar/VTE-padding pixel sizes from a terminal.

        Returns (char_w, char_h, sb_w, tb_h, vte_pad_x, vte_pad_y).
        Returns all zeros if the terminal is not yet allocated.
        """
        try:
            char_w = terminal.vte.get_char_width()
            char_h = terminal.vte.get_char_height()
            alloc = terminal.vte.get_allocation()
            if char_w <= 0 or char_h <= 0 or alloc.width <= char_w or alloc.height <= char_h:
                return 0, 0, 0, 0, 0, 0
            # Scrollbar is overlaid (Gtk.Overlay) — it doesn't consume
            # layout space, so sb_w is always 0 for pixel calculations.
            sb_w = 0
            # Titlebar: only counts if packed in the Terminal VBox
            # (consuming layout space). When overlaid (tmux mode), the
            # titlebar's parent is the Overlay, not the Terminal VBox.
            tb_h = 0
            if (hasattr(terminal, 'titlebar') and terminal.titlebar
                    and terminal.titlebar.get_visible()
                    and terminal.titlebar.get_parent() == terminal):
                tb_h = terminal.titlebar.get_allocation().height
            # VTE padding is zeroed globally via CSS (vte-terminal { padding: 0 }).
            # Don't query style context — per-VTE centering CSS overrides
            # would be read back and corrupt sizing calculations.
            vte_pad_x = 0
            vte_pad_y = 0
            return char_w, char_h, sb_w, tb_h, vte_pad_x, vte_pad_y
        except Exception:
            return 0, 0, 0, 0, 0, 0

    def _get_handle_size(self, terminal):
        """Get Paned handle size by walking up from a terminal."""
        w = terminal.get_parent()
        while w is not None:
            if hasattr(w, 'get_handlesize'):
                return w.get_handlesize()
            w = w.get_parent()
        return 0

    def _find_root_paned(self, terminal):
        """Find the highest Paned ancestor (the content container)."""
        root_paned = None
        w = terminal.get_parent()
        while w is not None:
            if hasattr(w, 'get_handlesize'):
                root_paned = w
            w = w.get_parent()
        return root_paned

    def _find_common_paned(self, term_a, term_b):
        """Find the Paned widget that is the direct common parent of two terminals."""
        # Walk up from term_a collecting parents
        parents_a = []
        w = term_a.get_parent()
        while w is not None:
            parents_a.append(w)
            w = w.get_parent()
        # Walk up from term_b and find first match
        w = term_b.get_parent()
        while w is not None:
            if w in parents_a:
                return w
            w = w.get_parent()
        return None

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
            new_terminal._make_titlebar_overlay()
            self.controller.register_terminal(pane_id, new_terminal)

            # Capture initial content
            self.protocol.send_command(
                'capture-pane -J -p -t {} -e -S - -E -'.format(pane_id),
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
            'list-windows -F "W:#{{window_id}}:#{{window_index}}:#{{window_name}}:#{{window_layout}}" -f "#{{==:#{{window_id}},{wid}}}"'.format(
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
            parts = rest.split(':', 3)
            if len(parts) < 4:
                continue
            wid = parts[0]
            window_index = parts[1]
            window_name = parts[2]
            layout_string = parts[3]
            self.controller.window_layouts[window_id] = layout_string
            self.controller.window_names[window_id] = window_name
            self.controller.window_indices[window_id] = window_index
            try:
                tree = parse_tmux_layout(layout_string)
                self._layout_trees[window_id] = tree
            except ValueError as e:
                dbg('TmuxHandlers: parse error for new window %s: %s' % (window_id, e))
                return
            GLib.idle_add(self._create_tab_for_window, window_id, tree)
            return

    def _create_tab_for_window(self, window_id, tree):
        """Create a new Terminator tab for a tmux window. Called on GTK thread.

        Builds the full split tree with correct sizes matching tmux's
        actual pane dimensions.
        """
        from terminatorlib.factory import Factory
        from terminatorlib.terminator import Terminator

        term = Terminator()
        maker = Factory()

        # Create the first terminal from the first leaf
        first_pane_id = self._first_leaf(tree).pane_id
        root_terminal = maker.make('Terminal')
        root_terminal.tmux_pane_id = first_pane_id
        root_terminal._make_titlebar_overlay()
        self.controller.register_terminal(first_pane_id, root_terminal)

        # Add as a new tab
        window = self._find_tmux_window(term)
        if not window:
            dbg('TmuxHandlers: no tmux window to add tab to')
            return False

        if not window.is_child_notebook():
            Factory().make('Notebook', window=window)
        notebook = window.get_child()
        notebook.newtab(widget=root_terminal)

        # Set tab label to index:name
        name = self.controller.window_names.get(window_id)
        index = self.controller.window_indices.get(window_id, '')
        if name:
            tab_label = '%s:%s' % (index, name) if index else name
            tab_root = notebook.find_tab_root(root_terminal)
            if tab_root:
                label = notebook.get_tab_label(tab_root)
                if label:
                    label.set_label(tab_label)

        # Now build the rest of the split tree
        if not tree.is_leaf:
            self._build_split_tree(tree, root_terminal, maker)

        # Don't capture pane content for new tabs — live %output
        # will deliver the prompt. Capturing would duplicate it.
        return False

    def _first_leaf(self, node):
        """Find the first leaf node in a layout tree."""
        if node.is_leaf:
            return node
        return self._first_leaf(node.children[0])

    def _build_split_tree(self, node, terminal, maker):
        """Recursively split terminals to match the tmux layout tree.

        Starting from a single terminal that represents the first leaf,
        split it for each additional child in the layout node.
        """
        if node.is_leaf:
            return

        # The terminal currently represents the first child.
        # For each subsequent child, split from the previous terminal.
        current_terminal = terminal
        for i in range(1, len(node.children)):
            child = node.children[i]
            first_leaf = self._first_leaf(child)

            new_terminal = maker.make('Terminal')
            new_terminal.tmux_pane_id = first_leaf.pane_id
            new_terminal._make_titlebar_overlay()
            self.controller.register_terminal(first_leaf.pane_id, new_terminal)

            # vertical=True means VPaned (top/bottom split) = tmux 'v' orientation
            vertical = node.orientation == 'v'

            # Calculate ratio: size of everything before this child / total
            if vertical:
                prev_size = sum(node.children[j].height for j in range(i))
                total_size = sum(c.height for c in node.children)
            else:
                prev_size = sum(node.children[j].width for j in range(i))
                total_size = sum(c.width for c in node.children)

            parent = current_terminal.get_parent()
            parent.split_axis(current_terminal, vertical=vertical,
                              sibling=new_terminal, widgetfirst=True)

            # Set the ratio on the newly created paned container
            paned = current_terminal.get_parent()
            if hasattr(paned, 'ratio') and total_size > 0:
                # For the i-th split, ratio is first_child / (first + second)
                first_child = node.children[i - 1]
                if vertical:
                    ratio = first_child.height / (first_child.height + child.height)
                else:
                    ratio = first_child.width / (first_child.width + child.width)
                paned.ratio = ratio
                paned.set_position_by_ratio()

            # If the child itself has sub-splits, recurse
            if not child.is_leaf:
                self._build_split_tree(child, new_terminal, maker)

            # The current terminal for the next iteration stays the same
            # (we always split from the last terminal added)
            current_terminal = new_terminal

        # Also recurse into the first child if it has sub-splits
        first_child = node.children[0]
        if not first_child.is_leaf:
            self._build_split_tree(first_child, terminal, maker)

    def on_window_close(self, info):
        """Handle %window-close: close all terminals in that window."""
        window_id = info.get('window_id', '')
        tmux_dbg('window-close: %s (known trees: %s)' % (
            window_id, list(self._layout_trees.keys())))
        tree = self._layout_trees.pop(window_id, None)
        self.controller.window_layouts.pop(window_id, None)
        if tree:
            pane_ids = get_pane_ids(tree)
            tmux_dbg('closing panes: %s' % pane_ids)
            GLib.idle_add(self._close_panes, pane_ids)
        else:
            dbg('TmuxHandlers: no tree found for window %s' % window_id)

    def on_window_renamed(self, info):
        """Handle %window-renamed: update tab title."""
        window_id = info.get('window_id', '')
        name = info.get('name', '')
        dbg('TmuxHandlers: window-renamed: %s -> %s' % (window_id, name))
        self.controller.window_names[window_id] = name
        if name:
            GLib.idle_add(self._update_tab_label, window_id, name)

    def _update_tab_label(self, window_id, name):
        """Update the tab label for a tmux window. Called on GTK thread."""
        tree = self._layout_trees.get(window_id)
        if not tree:
            return False
        # Format as "index:name" to match tmux status bar convention
        index = self.controller.window_indices.get(window_id, '')
        tab_label = '%s:%s' % (index, name) if index else name
        # Store window name and index on all terminals in this window
        for pane_id in get_pane_ids(tree):
            t = self.controller.pane_to_terminal.get(pane_id)
            if t:
                t._tmux_window_name = name
                t._tmux_window_index = index
        first_pane_id = self._first_leaf(tree).pane_id
        terminal = self.controller.pane_to_terminal.get(first_pane_id)
        if not terminal:
            return False
        widget = terminal.get_parent()
        while widget is not None:
            if hasattr(widget, 'find_tab_root'):
                tab_root = widget.find_tab_root(terminal)
                if tab_root:
                    label = widget.get_tab_label(tab_root)
                    if label and label.get_label() != tab_label:
                        label.set_label(tab_label)
                break
            widget = widget.get_parent()
        return False

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
            # Format: W:@WINDOW_ID:WINDOW_INDEX:WINDOW_NAME:LAYOUT_STRING
            if not decoded.startswith('W:@'):
                dbg('TmuxHandlers: skipping invalid line: %s' % decoded)
                continue
            # Strip the W: prefix, split on colons
            rest = decoded[2:]  # "@WINDOW_ID:WINDOW_INDEX:WINDOW_NAME:LAYOUT_STRING"
            parts = rest.split(':', 3)
            if len(parts) < 4:
                continue
            window_id = parts[0]
            window_index = parts[1]
            window_name = parts[2]
            layout_string = parts[3]
            self.controller.window_layouts[window_id] = layout_string
            self.controller.window_names[window_id] = window_name
            self.controller.window_indices[window_id] = window_index
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
        """Capture and display initial pane content after terminals are registered.

        Sends the initial resize command immediately, but defers the
        actual content capture until after the first _update_pane_sizes
        applies ratios. This ensures VTEs are at the correct size when
        captured content is fed, preventing wrapping artifacts.
        """
        self._send_initial_resize()
        self._capture_after_ratios = True
        self._title_timer = GLib.timeout_add(3000, self._periodic_title_refresh)

    def _send_captures(self):
        """Send capture-pane commands for all panes.

        Called after ratio application so VTEs are at the correct size
        when captured content is fed. Used on initial attach and after
        external resize (e.g. another tmux client changed the layout).
        """
        tmux_dbg('sending capture-pane commands (post-ratios)')
        for window_id, tree in self._layout_trees.items():
            for pane_id in get_pane_ids(tree):
                self.protocol.send_command(
                    'capture-pane -J -p -t {} -e -S - -E -'.format(pane_id),
                    callback=lambda result, pid=pane_id: self._feed_initial_capture(pid, result),
                )
        self._refresh_pane_titles()
        self._refresh_tab_labels()

    def _resize_window_to_tree(self, tree):
        """Resize our GTK window to match tmux's layout tree dimensions.

        Called on GTK thread when tmux changes the layout size (e.g.
        another client attached with a different window size).
        """
        import time
        terminals = list(self.controller.terminal_to_pane.keys())
        if not terminals:
            return False

        term = None
        char_w = char_h = sb_w = tb_h = vpad_x = vpad_y = 0
        for t in terminals:
            char_w, char_h, sb_w, tb_h, vpad_x, vpad_y = \
                self._get_terminal_metrics(t)
            if char_w > 0:
                term = t
                break
        if not term or char_w <= 0:
            return False

        handle_size = self._get_handle_size(term)

        target_w = self._subtree_px(tree, 'h', char_w, char_h, sb_w, tb_h,
                                     handle_size, vpad_x, vpad_y)
        target_h = self._subtree_px(tree, 'v', char_w, char_h, sb_w, tb_h,
                                     handle_size, vpad_x, vpad_y)

        window = term.get_toplevel()
        root_paned = self._find_root_paned(term)
        chrome_w = chrome_h = 0
        content = window.get_child()
        if content and root_paned:
            content_alloc = content.get_allocation()
            paned_alloc = root_paned.get_allocation()
            chrome_w = content_alloc.width - paned_alloc.width
            chrome_h = content_alloc.height - paned_alloc.height

        win_w = int(target_w) + chrome_w
        win_h = int(target_h) + chrome_h

        tmux_dbg('resize window to tree: %dx%d -> %dx%dpx '
                 '(chrome=%dx%d char=%dx%d handle=%d)' % (
                     tree.width, tree.height, win_w, win_h,
                     chrome_w, chrome_h, char_w, char_h, handle_size))

        window.resize(win_w, win_h)
        # Update pixel tracking so notify_resize detects this as a
        # window resize (not a split drag) when VTE sizes change
        self.controller._last_window_pixels = (win_w, win_h)
        self.controller._layout_applied_time = time.monotonic()
        # Recapture content after ratios are applied for the new size
        self._capture_after_ratios = True
        return False

    def _send_initial_resize(self):
        """Size our window to match tmux's layout, then tell tmux our size.

        Uses actual VTE char metrics + scrollbar/titlebar/handle sizes to
        compute the correct pixel dimensions. If the layout won't fit on
        screen, clamps to screen size and tells tmux to use the smaller
        dimensions.
        """
        tree = None
        for tree in self._layout_trees.values():
            break
        if tree is None:
            return

        # Get metrics from an allocated terminal
        terminals = list(self.controller.terminal_to_pane.keys())
        char_w = char_h = sb_w = tb_h = vpad_x = vpad_y = 0
        term = None
        for t in terminals:
            char_w, char_h, sb_w, tb_h, vpad_x, vpad_y = \
                self._get_terminal_metrics(t)
            if char_w > 0:
                term = t
                break
        if not term or char_w <= 0:
            # Terminals not realized yet — fall back to tmux dimensions
            tmux_dbg('initial resize to %dx%d (from tmux layout, '
                     'no metrics yet)' % (tree.width, tree.height))
            self.protocol.send_command(
                'refresh-client -C {},{}'.format(tree.width, tree.height))
            return

        handle_size = self._get_handle_size(term)

        # Compute pixel dimensions for tmux's layout (content area)
        target_w = self._subtree_px(tree, 'h', char_w, char_h, sb_w, tb_h,
                                     handle_size, vpad_x, vpad_y)
        target_h = self._subtree_px(tree, 'v', char_w, char_h, sb_w, tb_h,
                                     handle_size, vpad_x, vpad_y)

        # Chrome between window content area and root Paned.
        # window.resize() sets the content child (e.g. Notebook) size.
        # The root Paned is inside that, minus borders and tab bar.
        window = term.get_toplevel()
        root_paned = self._find_root_paned(term)
        chrome_w = chrome_h = 0
        content = window.get_child()
        if content and root_paned:
            content_alloc = content.get_allocation()
            paned_alloc = root_paned.get_allocation()
            chrome_w = content_alloc.width - paned_alloc.width
            chrome_h = content_alloc.height - paned_alloc.height

        # Get screen limits
        from gi.repository import Gdk
        screen = window.get_screen()
        monitor = screen.get_monitor_at_window(window.get_window()) \
            if window.get_window() else 0
        mon_geom = screen.get_monitor_workarea(monitor)
        max_w = mon_geom.width
        max_h = mon_geom.height

        # Target: paned content + chrome between content child and paned
        target_win_w = int(target_w) + chrome_w
        target_win_h = int(target_h) + chrome_h
        fits = target_win_w <= max_w and target_win_h <= max_h
        win_w = min(target_win_w, max_w)
        win_h = min(target_win_h, max_h)

        tmux_dbg('initial sizing: tmux=%dx%d target_paned=%dx%dpx '
                 'chrome=%dx%d target_win=%dx%d screen=%dx%d fits=%s '
                 'char=%dx%d sb=%d tb=%d handle=%d vte_pad=%dx%d' % (
                     tree.width, tree.height, target_w, target_h,
                     chrome_w, chrome_h, win_w, win_h,
                     max_w, max_h, fits,
                     char_w, char_h, sb_w, tb_h,
                     handle_size, vpad_x, vpad_y))

        window.resize(win_w, win_h)

        if fits:
            # Tell tmux we match its layout
            tmux_dbg('initial resize to %dx%d (matches tmux)' % (
                tree.width, tree.height))
            self.protocol.send_command(
                'refresh-client -C {},{}'.format(tree.width, tree.height))
        else:
            # Screen too small — compute what we can fit and tell tmux
            # to downsize. Back-calculate character dimensions from pixels.
            fit_cols = (win_w - vpad_x - sb_w) // char_w
            fit_rows = (win_h - vpad_y - tb_h) // char_h
            tmux_dbg('initial resize to %dx%d (screen limited from %dx%d)' % (
                fit_cols, fit_rows, tree.width, tree.height))
            self.protocol.send_command(
                'refresh-client -C {},{}'.format(fit_cols, fit_rows))

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

    def _periodic_title_refresh(self):
        """Periodically refresh pane titles from tmux.

        Tmux intercepts title-setting escape sequences (OSC 0/2) and
        does not forward them via %output, so VTE never sees them.
        Polling is the only way to get updated pane titles.
        """
        if not self.controller.active:
            return False
        self._refresh_pane_titles()
        self._refresh_tab_labels()
        return True

    def _refresh_tab_labels(self):
        """Set tab labels and window title from stored tmux window names."""
        for window_id, name in self.controller.window_names.items():
            if name:
                GLib.idle_add(self._update_tab_label, window_id, name)
        # Set the window title to the session name
        if self.controller.session_name:
            GLib.idle_add(self._set_window_title,
                          'tmux: %s' % self.controller.session_name)

    def _set_window_title(self, title):
        """Set the Terminator window title. Called on GTK thread."""
        from terminatorlib.terminator import Terminator
        try:
            term = Terminator()
            window = self._find_tmux_window(term)
            if window:
                window.title.force_title(title)
        except Exception:
            pass
        return False

    def _refresh_pane_titles(self):
        """Query tmux for pane titles and update terminal titlebars."""
        self.protocol.send_command(
            'list-panes -s -F "#{pane_id}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_title}"',
            callback=self._on_pane_titles,
        )

    def _on_pane_titles(self, result):
        """Handle pane title query response."""
        if result.is_error:
            return
        import os
        home = os.environ.get('HOME', '')
        for line in result.output_lines:
            decoded = line.decode('utf-8', errors='replace').strip()
            parts = decoded.split('\t', 3)
            if len(parts) < 4:
                continue
            pane_id, command, path, pane_title = parts
            terminal = self.controller.pane_to_terminal.get(pane_id)
            if not terminal:
                continue
            # Shorten home prefix to ~
            if home and path.startswith(home):
                path = '~' + path[len(home):]
            # Get last path component (or ~ for home)
            if path == '~' or path == '/':
                short_path = path
            else:
                short_path = os.path.basename(path)
            # Smart title: shells show just dir name, others show "command: dir"
            if command in SHELL_COMMANDS:
                title = short_path
            else:
                title = '%s: %s' % (command, short_path) if short_path else command
            GLib.idle_add(self._set_terminal_title, terminal, title)

    def _set_terminal_title(self, terminal, title):
        """Set a terminal's titlebar text. Must be called on GTK thread.

        Only updates the pane titlebar directly — does NOT emit
        title-change, which would override the tab label with the
        pane title instead of the tmux window name.
        """
        try:
            old_title = getattr(terminal, '_tmux_title', None)
            if title == old_title:
                return False
            terminal._tmux_title = title
            if hasattr(terminal, 'titlebar'):
                terminal.titlebar.set_terminal_title(None, title)
        except Exception:
            pass
        return False
