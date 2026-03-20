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

SHELL_COMMANDS = {'bash', 'zsh', 'fish', 'sh', 'dash', 'csh', 'tcsh', 'ksh'}


class TmuxHandlers:
    """Handles tmux notifications and maps them to Terminator operations."""

    def __init__(self, controller):
        self.controller = controller
        self.protocol = controller.protocol
        self._layout_trees = {}  # window_id -> LayoutNode tree
        self._needs_ratio_retry = False
        self._reconcile_timer = None
        self._initial_capture_pending = False
        self._tmux_paneds = set()  # all tmux-managed paned widgets
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
            # Buffer output for panes not yet registered (race: %output
            # arrives before _create_tab_for_window finishes).
            buf = self.controller._pending_output
            if pane_id not in buf:
                buf[pane_id] = []
            buf[pane_id].append(data)
            dbg('TmuxHandlers: buffered %output for unregistered pane %s (%d chunks)' % (pane_id, len(buf[pane_id])))
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

    def _get_chrome_size(self, window):
        """Compute chrome pixels (tab bar, borders) excluding pane area.

        Chrome = content_allocation - notebook_page_allocation.
        Using the notebook page (not a terminal) avoids counting
        other panes as chrome when splits are present.

        Falls back to preferred sizes when allocations aren't
        available yet (before GTK's first layout pass).
        """
        content = window.get_child()
        if not content:
            return 0, 0
        ca = content.get_allocation()
        if hasattr(content, 'get_current_page'):
            page_num = content.get_current_page()
            if page_num >= 0:
                page = content.get_nth_page(page_num)
                pa = page.get_allocation()
                cw = ca.width - pa.width
                ch = ca.height - pa.height
                if cw > 0 or ch > 0:
                    return cw, ch
                # Allocations not ready — use preferred sizes
                _, cnt_w = content.get_preferred_width()
                _, cnt_h = content.get_preferred_height()
                _, pg_w = page.get_preferred_width()
                _, pg_h = page.get_preferred_height()
                return max(0, cnt_w - pg_w), max(0, cnt_h - pg_h)
        return 0, 0

    def _find_tmux_window(self, terminator):
        """Find the Terminator window that contains this controller's terminals."""
        for window in terminator.windows:
            for terminal in window.get_terminals():
                if terminal in self.controller.terminal_to_pane:
                    return window
        return None

    def _is_active_window(self, tree, window_id=None):
        """Check if this layout tree's panes are in the currently visible tab."""
        # Check tmux's active window flag first (reliable before
        # terminals are mapped and during rapid resize sequences)
        if window_id is None:
            awid = self.controller.active_window_id
            if awid:
                for wid, t in self._layout_trees.items():
                    if t is tree:
                        return wid == awid
        elif self.controller.active_window_id:
            return window_id == self.controller.active_window_id
        # Fall back to GTK mapped state
        pane_ids = get_pane_ids(tree)
        for pid in pane_ids:
            terminal = self.controller.pane_to_terminal.get(pid)
            if terminal and terminal.get_mapped():
                return True
        return False

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
        dbg('layout-change: %s old=%s new=%s client=%s '
                 'del=%s add=%s elapsed=%.3fs applying=%s' % (
                     window_id, old_dims, new_dims, client_size,
                     deleted_panes or '{}', added_panes or '{}',
                     elapsed, self.controller._applying_layout))

        self._layout_trees[window_id] = new_tree
        self.controller.window_layouts[window_id] = layout_string

        # Trace window size at layout-change entry
        for t in list(self.controller.terminal_to_pane.keys())[:1]:
            try:
                top = t.get_toplevel()
                wa = top.get_allocation()
                ws = top.get_size()
                va = t.vte.get_allocation()
                vc = t.vte.get_column_count()
                vr = t.vte.get_row_count()
                dbg('size_trace layout-change: '
                    'alloc=%dx%d ws=%dx%d vte=%dx%d '
                    'chars=%dx%d tree=%dx%d' % (
                    wa.width, wa.height, ws[0], ws[1],
                    va.width, va.height, vc, vr,
                    new_tree.width, new_tree.height))
            except Exception:
                pass

        if deleted_panes:
            GLib.idle_add(self._close_panes, deleted_panes)
        elif added_panes:
            GLib.idle_add(self._add_panes, added_panes, new_tree)
        else:
            # Only process resize/constraint logic for the active
            # window (panes currently visible). Background windows
            # (e.g. @30 at 132x40) must not resize us or set MAX.
            active = self._is_active_window(new_tree, window_id)
            if not active:
                dbg('layout-change: skipping resize for background '
                    'window %s' % window_id)
                return

            new_size = (new_tree.width, new_tree.height)
            we_caused_it = elapsed < 1.0
            if client_size and new_size != client_size and not we_caused_it:
                dbg('layout-change: unsolicited size change '
                         '%dx%d -> %dx%d (elapsed=%.3fs), resizing window' % (
                             client_size[0], client_size[1],
                             new_size[0], new_size[1], elapsed))
                self.controller._tmux_max_cols = None
                self.controller._tmux_max_rows = None
                GLib.idle_add(self._resize_window_to_tree, new_tree)
            elif client_size and new_size != client_size:
                rejected = (new_size[0] < client_size[0] or
                            new_size[1] < client_size[1])
                if rejected:
                    dbg('layout-change: tmux rejected %dx%d -> %dx%d '
                             '(elapsed=%.3fs), re-constraining' % (
                                 client_size[0], client_size[1],
                                 new_size[0], new_size[1], elapsed))
                    # Don't clear max or call _resize_window_to_tree
                    # here — _update_max_from_tree will set the hard MAX hint
                    # and the WM enforces the constraint. Snapping back during
                    # drag fights the mouse and causes jitter.
                else:
                    dbg('layout-change: echo-back size change '
                             '%dx%d -> %dx%d (elapsed=%.3fs)' % (
                                 client_size[0], client_size[1],
                                 new_size[0], new_size[1], elapsed))
            else:
                # Confirmed match or initial state — snap window
                # to match tmux's layout (catches chrome changes
                # like tab bar appearing).
                GLib.idle_add(self._resize_window_to_tree, new_tree)

            # Update max size from tree dimensions (free, no query)
            GLib.idle_add(self._update_max_from_tree,
                          new_tree.width, new_tree.height)

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
                    dbg('  %s%s: tmux=%dx%d widget=%dx%d vte=%dx%d %s' % (
                        '  ' * depth, node.pane_id, node.width, node.height,
                        tw_cols, tw_rows, vte_cols, vte_rows, match))
                except Exception:
                    pass
        else:
            dbg('  %s%s split %dx%d:' % ('  ' * depth, node.orientation, node.width, node.height))
            for child in node.children:
                self._log_layout_sizes(child, depth + 1)

    def _update_pane_sizes(self, tree):
        """Update split ratios to match tmux's pane dimensions.
        Called on GTK thread."""
        import time
        # Trace window size at pane-size update
        for t in list(self.controller.terminal_to_pane.keys())[:1]:
            try:
                top = t.get_toplevel()
                wa = top.get_allocation()
                ws = top.get_size()
                va = t.vte.get_allocation()
                vc = t.vte.get_column_count()
                vr = t.vte.get_row_count()
                dbg('size_trace update_pane_sizes: '
                    'alloc=%dx%d ws=%dx%d vte=%dx%d '
                    'chars=%dx%d tree=%dx%d' % (
                    wa.width, wa.height, ws[0], ws[1],
                    va.width, va.height, vc, vr,
                    tree.width, tree.height))
            except Exception:
                pass
        self.controller._applying_layout = True
        dbg('_apply_layout_to_tree: set _applying_layout=True')
        deferred = False
        try:
            self._record_tmux_sizes(tree)
            self._ratios_changed = False
            if not tree.is_leaf:
                self._needs_ratio_retry = False
                self._apply_ratios(tree)
                if self._needs_ratio_retry:
                    dbg('retrying ratios in 100ms (unallocated paneds)')
                    GLib.timeout_add(100, self._apply_ratios_and_finish, tree)
                    deferred = True
        finally:
            if not deferred:
                import time
                self.controller._layout_applied_time = time.monotonic()
                if self._ratios_changed:
                    # Ratios changed → VTE allocations will change →
                    # notify_resize will fire and clear the flag
                    # reactively via _finish_applying_layout.
                    self._pending_layout_tree = tree
                else:
                    # No ratios changed (single pane or same layout).
                    # No VTE allocations will change, so nothing will
                    # trigger _finish_applying_layout.  Defer to idle
                    # so GTK can process pending allocations (e.g.
                    # notebook tab bar) before the chrome check runs.
                    GLib.idle_add(self._finish_applying_layout, tree)
        return False

    def _apply_ratios_and_finish(self, tree):
        """Deferred callback to apply ratios for unallocated Paneds.

        _applying_layout stays True until all ratios are applied,
        providing continuous suppression of resize echo-back.
        """
        self._needs_ratio_retry = False
        self._ratios_changed = False
        try:
            self._apply_ratios(tree)
            if self._needs_ratio_retry:
                dbg('retrying ratios in 100ms (unallocated paneds)')
                GLib.timeout_add(100, self._apply_ratios_and_finish, tree)
                return False  # keep _applying_layout True
        except Exception:
            pass
        import time
        self.controller._layout_applied_time = time.monotonic()
        if self._ratios_changed:
            self._pending_layout_tree = tree
        else:
            GLib.idle_add(self._finish_applying_layout, tree)
        return False

    def _finish_applying_layout(self, tree):
        """Clear _applying_layout after deferred VTE size-allocate
        handlers have been processed.

        Runs at priority DEFAULT_IDLE+10 (210), which is lower than
        the deferred notify_resize at DEFAULT_IDLE (200).  This
        ensures _applying_layout stays True long enough to suppress
        the stale resize echo-back that would otherwise send a
        refresh-client with pre-ratio VTE column counts.
        """
        import time
        dbg('_finish_applying_layout: clearing _applying_layout')
        self.controller._applying_layout = False
        self.controller._layout_applied_time = time.monotonic()
        initial = self.controller._last_client_size is None
        if not initial:
            # We just applied a layout from tmux — the tree
            # dimensions ARE the authoritative client size.
            # Don't use _calculate_client_size() here: VTE pixel
            # rounding means the sum of VTE chars can exceed the
            # tree, triggering a grow loop (tree→VTE→bigger
            # refresh-client→bigger tree→repeat).
            if tree and tree.width > 0 and tree.height > 0:
                size = (tree.width, tree.height)
                if size != self.controller._last_client_size:
                    dbg('_finish_applying_layout: client size '
                        'changed %dx%d -> %dx%d (from tree)' % (
                        self.controller._last_client_size[0],
                        self.controller._last_client_size[1],
                        size[0], size[1]))
                    self.controller._last_client_size = size
                    self.controller.protocol.send_command(
                        'refresh-client -C {},{}'.format(
                            size[0], size[1]))
                    self.controller._layout_applied_time = \
                        time.monotonic()
            self._snapshot_vte_sizes()
        else:
            # Initial startup: VTE hasn't settled into the
            # resized window yet — paned position changes from
            # _apply_ratios are still pending allocation.
            # Skip reconcile; the natural flow (VTE settles →
            # notify_resize → do_resize → refresh-client →
            # layout-change) will converge without it.
            cols = tree.width if tree else 0
            rows = tree.height if tree else 0
            if cols > 0 and rows > 0:
                size = (cols, rows)
                self.controller._last_client_size = size
                dbg('_finish_applying_layout: initial '
                    'client size %d,%d (from tree)' % (cols, rows))
                self.controller._refresh_layout_state()
                if self._initial_capture_pending:
                    self._initial_capture_pending = False
                    self._send_initial_captures()
                self._snapshot_vte_sizes()
        # Check for chrome change (e.g. tab bar appeared) before
        # reconcile.  If chrome changed, update geometry hints first
        # (so the WM snaps to the correct grid), then resize the
        # window.  Skip reconcile — the resize will trigger fresh
        # VTE allocations that produce correct sizes.
        chrome_changed = False
        for t in list(self.controller.terminal_to_pane.keys())[:1]:
            try:
                top = t.get_toplevel()
                chrome = self._get_chrome_size(top)
                if (self.controller._last_chrome is not None
                        and chrome != self.controller._last_chrome):
                    dbg('_finish_applying_layout: chrome_changed '
                        '%dx%d -> %dx%d' % (
                        self.controller._last_chrome[0],
                        self.controller._last_chrome[1],
                        chrome[0], chrome[1]))
                    self.controller._last_chrome = chrome
                    # Update hints before resize so WM uses new BASE
                    top.set_tmux_geometry_hints(t)
                    self._resize_window_to_tree(tree)
                    self.controller._layout_applied_time = \
                        time.monotonic()
                    chrome_changed = True
            except Exception:
                pass
            break

        # No reconcile — refresh-client -C tells tmux the correct
        # total size; tmux distributes panes.  Reconcile's per-pane
        # resize-pane commands caused a feedback loop (resize-pane →
        # layout-change → reconcile → resize-pane → ...) that made
        # pane proportions jump during window resize.
        return False  # don't repeat

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
        """Schedule reconciliation after all pending allocations.

        Uses idle priority below _finish_applying_layout (210) so
        VTE size-allocate handlers have already run by the time
        reconcile executes — no timer race.
        """
        if self._reconcile_timer:
            GLib.source_remove(self._reconcile_timer)
        self._reconcile_timer = GLib.idle_add(
            self._reconcile_pane_sizes, tree,
            priority=GLib.PRIORITY_DEFAULT_IDLE + 20)

    def _reconcile_pane_sizes(self, tree):
        """Send resize-pane for any pane where VTE is smaller than tmux."""
        import time
        self._reconcile_timer = None
        client_size = self.controller._last_client_size
        dbg('reconcile: tree=%dx%d client=%s' % (
            tree.width, tree.height, client_size))
        if client_size and (tree.width > client_size[0]
                            or tree.height > client_size[1]):
            dbg('reconcile: skipping — layout %dx%d '
                'exceeds client %dx%d' % (
                tree.width, tree.height,
                client_size[0], client_size[1]))
            return False

        mismatches = []
        self._collect_mismatches(tree, mismatches)
        if not mismatches:
            dbg('reconcile: all panes match tmux')
            return False
        dbg('reconcile: fixing %d mismatched pane(s):' % len(mismatches))
        for pane_id, vte_cols, vte_rows, tmux_cols, tmux_rows in mismatches:
            dbg('  %s: vte=%dx%d tmux=%dx%d' % (
                pane_id, vte_cols, vte_rows, tmux_cols, tmux_rows))
            parts = ['resize-pane -t {}'.format(pane_id)]
            # Only shrink axes — never grow tmux panes
            if vte_cols < tmux_cols:
                parts.append('-x {}'.format(vte_cols))
            if vte_rows < tmux_rows:
                parts.append('-y {}'.format(vte_rows))
            if len(parts) == 1:
                continue  # nothing to shrink
            cmd = ' '.join(parts)
            dbg('reconcile: %s' % cmd)
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
        """Collect panes where VTE is smaller than tmux expects.

        Only shrink direction: if VTE has fewer cols/rows than
        tmux's tree, report it so tmux can reallocate.  Never
        report VTE > tmux — that's pixel rounding slack and
        growing tmux panes causes a feedback loop.
        """
        if node.is_leaf:
            terminal = self.controller.pane_to_terminal.get(node.pane_id)
            if terminal:
                try:
                    vte_cols = terminal.vte.get_column_count()
                    vte_rows = terminal.vte.get_row_count()
                    if (vte_cols < node.width
                            or vte_rows < node.height):
                        out.append((node.pane_id, vte_cols,
                                    vte_rows, node.width,
                                    node.height))
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

            # Mark as tmux-managed (disables snap fighting).
            paned._tmux_managed = True

            # Tell GTK to keep child1 at its set position when
            # the paned is resized — don't proportionally rescale.
            # Without this, gtk_paned_calc_position does
            # pos = new_alloc * old_pos / old_alloc, which negates
            # any position we set in do_size_allocate.
            child1 = paned.get_child1()
            child2 = paned.get_child2()
            if child1:
                paned.child_set_property(
                    child1, 'resize', False)
            if child2:
                paned.child_set_property(
                    child2, 'resize', True)

            handle_size = paned.get_handlesize()
            paned_len = paned.get_length()

            if paned_len <= handle_size:
                dbg('ratio SKIPPED: paned not allocated '
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

            # Log tree node dimensions vs paned allocation
            left_node = remaining[0]
            right_nodes = remaining[1:]
            left_dim = ('%dx%d' % (left_node.width,
                                    left_node.height)
                        if left_node.is_leaf
                        else '%s(%dx%d)' % (
                            left_node.orientation,
                            left_node.width,
                            left_node.height))
            right_dim = ','.join(
                ('%dx%d' % (c.width, c.height)
                 if c.is_leaf
                 else '%s(%dx%d)' % (
                     c.orientation, c.width, c.height))
                for c in right_nodes)
            total_needed = left_px + right_px + handle_size
            stale = total_needed > paned_len + handle_size
            dbg('ratio tree: left=[%s] right=[%s] '
                'need=%dpx paned=%dpx %s' % (
                left_dim, right_dim,
                total_needed, paned_len,
                'STALE' if stale else 'ok'))

            # Anchor child2 at its exact target pixel size so that
            # only child1 (the pane bordering the handle) absorbs
            # size changes — the far pane stays fixed.  The gap
            # between the real handle and a full character cell
            # naturally becomes dead space in child1.
            #
            # When the paned is stale (not yet allocated at the
            # new window size), use proportional positioning so
            # the split looks approximately correct while waiting
            # for the anchor to fix it on the next allocation.
            if stale and total_needed > 0:
                ratio = left_px / float(total_needed)
                usable = paned_len - handle_size
                target_pos = max(0, int(ratio * usable))
            else:
                target_pos = paned_len - right_px - handle_size
                if target_pos < 0:
                    target_pos = 0

            # Detect if user is actively dragging THIS handle:
            # mouse button held + position differs from last sync
            # + length unchanged (not a parent reallocation).
            #
            # GTK3 button-release may not fire reliably on paneds,
            # so verify actual pointer state when flag is set.
            synced = getattr(paned, '_tmux_synced_pos', None)
            prev_len = getattr(paned, '_tmux_prev_len', None)
            cur_pos = paned.get_position()
            if getattr(paned, '_tmux_handle_pressed', False):
                try:
                    from gi.repository import Gdk
                    seat = paned.get_display().get_default_seat()
                    ptr = seat.get_pointer()
                    win = paned.get_window()
                    if win and ptr:
                        _, _, _, mask = \
                            win.get_device_position(ptr)
                        if not (mask
                                & Gdk.ModifierType.BUTTON1_MASK):
                            paned._tmux_handle_pressed = False
                except Exception:
                    pass
            user_dragging = (getattr(paned,
                                 '_tmux_handle_pressed', False)
                             and synced is not None
                             and prev_len is not None
                             and cur_pos != synced
                             and paned_len == prev_len)

            # Check if an ANCESTOR paned's handle is being
            # dragged — this paned's total size is changing
            # due to the parent drag, so let do_size_allocate
            # anchor handle it instead of overriding here.
            # Uses the same pointer-state verification as above.
            ancestor_dragging = False
            w = paned.get_parent()
            while w is not None:
                if getattr(w, '_tmux_handle_pressed', False):
                    # Verify button is actually held
                    try:
                        from gi.repository import Gdk
                        seat = w.get_display().get_default_seat()
                        ptr = seat.get_pointer()
                        wn = w.get_window()
                        if wn and ptr:
                            _, _, _, mask = \
                                wn.get_device_position(ptr)
                            if not (mask
                                    & Gdk.ModifierType
                                    .BUTTON1_MASK):
                                w._tmux_handle_pressed = False
                                w = w.get_parent()
                                continue
                    except Exception:
                        pass
                    ancestor_dragging = True
                    break
                w = w.get_parent()

            skip_tag = ''
            if user_dragging:
                skip_tag = ' SKIP(user dragging)'
            elif ancestor_dragging:
                skip_tag = ' SKIP(ancestor drag)'

            dbg('ratio %s-split: left=%dpx right=%dpx '
                     'pos=%d old_pos=%d paned=%d '
                     'char=%dx%d sb=%d tb=%d handle=%d '
                     'vte_pad=%dx%d%s' % (
                         orient, left_px, right_px, target_pos,
                         cur_pos, paned_len,
                         char_w, char_h, sb_w, tb_h,
                         handle_size, vpad_x, vpad_y,
                         skip_tag))

            if user_dragging:
                # Don't fight the user's drag — leave their
                # handle position alone.  Update synced_pos to
                # the tmux target so _send_split_bar_resize
                # knows the baseline for the next delta.
                paned._tmux_synced_pos = target_pos
            elif ancestor_dragging:
                # An ancestor's handle is being dragged — this
                # paned's total size is changing.  Let the
                # do_size_allocate anchor keep child2 locked
                # using pre-drag values.
                paned._tmux_synced_pos = target_pos
            elif abs(cur_pos - target_pos) > 0:
                paned.set_pos(target_pos)
                # Update stored ratio to match the new position
                paned.ratio = paned.ratio_by_position(
                    paned_len, handle_size, target_pos)
                self._ratios_changed = True
                paned._tmux_synced_pos = paned.get_position()
            else:
                paned._tmux_synced_pos = cur_pos

            # Record child2 target and current length so the
            # size-allocate handler can anchor child2 when a
            # parent drag changes this paned's total size.
            # Skip when an ancestor is being dragged — keep
            # pre-drag anchor values so the far divider stays.
            if not ancestor_dragging or user_dragging:
                paned._tmux_child2_px = right_px
                paned._tmux_prev_len = paned_len
            if not getattr(paned, '_tmux_anchor_connected', False):
                paned.connect('size-allocate',
                              self._on_tmux_paned_allocate)
                paned.connect('button-press-event',
                              self._on_paned_button_press)
                paned.connect('button-release-event',
                              self._on_paned_button_release)
                paned._tmux_anchor_connected = True

            # Store child1's pane_id (the terminal nearest the
            # handle on the child1 side).
            last_leaf_l = self._last_leaf(remaining[0])
            paned._tmux_child1_pane_id = last_leaf_l.pane_id
            self._tmux_paneds.add(paned)

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
        char_w and char_h are fractional (Pango-based) for accuracy —
        get_char_width()/get_char_height() truncate to int, and the
        error accumulates over many characters.
        Returns all zeros if the terminal is not yet allocated.
        """
        try:
            int_cw = terminal.vte.get_char_width()
            int_ch = terminal.vte.get_char_height()
            alloc = terminal.vte.get_allocation()
            if int_cw <= 0 or int_ch <= 0 \
                    or alloc.width <= int_cw \
                    or alloc.height <= int_ch:
                return 0, 0, 0, 0, 0, 0
            char_w = int_cw
            char_h = int_ch
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
            vte_pad_x = 0
            vte_pad_y = 0
            return char_w, char_h, sb_w, tb_h, vte_pad_x, vte_pad_y
        except Exception:
            return 0, 0, 0, 0, 0, 0

    def _on_tmux_paned_allocate(self, paned, allocation):
        """Anchor child2 when this paned is reallocated by a parent.

        Skipped when the user is dragging THIS paned's handle
        (they control the position). Active for all other cases:
        tmux layout changes AND parent handle drags — this keeps
        the far child locked so non-dragged dividers don't jitter.
        """
        if getattr(paned, '_tmux_handle_pressed', False):
            return
        child2_px = getattr(paned, '_tmux_child2_px', None)
        if child2_px is None:
            return
        cur_len = paned.get_length()
        prev_len = getattr(paned, '_tmux_prev_len', 0)
        if cur_len == prev_len:
            return  # length unchanged — no reallocation needed
        paned._tmux_prev_len = cur_len
        handle = paned.get_handlesize()
        new_pos = max(cur_len - child2_px - handle, 0)
        if new_pos != paned.get_position():
            paned.set_pos(new_pos)

    def _on_paned_button_press(self, paned, event):
        """Track when user starts dragging a paned handle.

        GTK propagates button-press from child paneds up to parents.
        Only set the flag if the click actually landed on THIS
        paned's handle — not on a descendant widget.
        """
        if event.button == 1:
            from gi.repository import Gtk
            target = Gtk.get_event_widget(event)
            if target is paned:
                paned._tmux_handle_pressed = True
        return False  # let GTK handle the drag

    def _on_paned_button_release(self, paned, event):
        """Track when user stops dragging a paned handle."""
        if event.button == 1:
            paned._tmux_handle_pressed = False
        return False

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

            # Don't capture-pane here — the shell prompt and any
            # output that arrived before registration are already
            # buffered in _pending_output and replayed by
            # register_terminal.  Capturing would duplicate them.

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

        # Set initial tab label from cached window name, then refresh
        # with full formatting (async query to tmux)
        name = self.controller.window_names.get(window_id, '')
        if name:
            tab_root = notebook.find_tab_root(root_terminal)
            if tab_root:
                label = notebook.get_tab_label(tab_root)
                if label:
                    label.set_label('[%s]' % name)
        self._refresh_tab_labels()

        # Now build the rest of the split tree
        if not tree.is_leaf:
            self._build_split_tree(tree, root_terminal, maker)

        return False

    def _first_leaf(self, node):
        """Find the first leaf node in a layout tree."""
        if node.is_leaf:
            return node
        return self._first_leaf(node.children[0])

    def _last_leaf(self, node):
        """Find the last leaf node in a layout tree."""
        if node.is_leaf:
            return node
        return self._last_leaf(node.children[-1])

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
        dbg('window-close: %s (known trees: %s)' % (
            window_id, list(self._layout_trees.keys())))
        tree = self._layout_trees.pop(window_id, None)
        self.controller.window_layouts.pop(window_id, None)
        if tree:
            pane_ids = get_pane_ids(tree)
            dbg('closing panes: %s' % pane_ids)
            GLib.idle_add(self._close_panes, pane_ids)
        else:
            dbg('TmuxHandlers: no tree found for window %s' % window_id)

    def on_window_renamed(self, info):
        """Handle %window-renamed: refresh tab labels from tmux."""
        window_id = info.get('window_id', '')
        name = info.get('name', '')
        dbg('TmuxHandlers: window-renamed: %s -> %s' % (window_id, name))
        self.controller.window_names[window_id] = name
        # Refresh with full formatting (command, path, custom title)
        self._refresh_tab_labels()

    def _update_tab_label(self, window_id, tab_label):
        """Update the tab label for a tmux window. Called on GTK thread."""
        tree = self._layout_trees.get(window_id)
        if not tree:
            return False
        # Store window name and index on all terminals in this window
        name = self.controller.window_names.get(window_id, '')
        index = self.controller.window_indices.get(window_id, '')
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
        # Restore termios immediately on the reader thread, before the
        # shell resumes and readline saves the wrong terminal state.
        # Close the duped fd to force the bridge reader to stop instantly
        # so it can't consume the shell's prompt from the PTY buffer.
        # The original fd (in _saved_pty) remains open for VTE.
        proto = self.controller.protocol
        if hasattr(proto, '_bridge'):
            proto._bridge.restore_termios()
            import os
            try:
                os.close(proto._bridge._fd)
            except OSError:
                pass
            proto._bridge._alive = False
        GLib.idle_add(self._handle_exit)

    def _handle_exit(self):
        """Handle tmux exit on GTK thread."""
        from terminatorlib.terminator import Terminator
        term = Terminator()
        window = self._find_tmux_window(term)
        # Close any remaining terminals (tmux may not send %window-close
        # for the last window before %exit)
        for terminal in list(self.controller.pane_to_terminal.values()):
            terminal._tmux_closing = True
            terminal.close()
        self.controller.stop(send_detach=False)
        # If the window still exists and has no children, destroy it
        if window:
            window.hoover()
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
            # Format: W:@WINDOW_ID:INDEX:NAME:ACTIVE:LAYOUT
            if not decoded.startswith('W:@'):
                dbg('TmuxHandlers: skipping invalid line: %s' % decoded)
                continue
            # Strip the W: prefix, split on colons
            rest = decoded[2:]
            parts = rest.split(':', 4)
            if len(parts) < 5:
                continue
            window_id = parts[0]
            window_index = parts[1]
            window_name = parts[2]
            window_active = parts[3]
            layout_string = parts[4]
            if window_active == '1':
                self.controller.active_window_id = window_id
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
        """Set up initial state after terminals are registered.

        Sends the initial resize command, captures existing pane content
        once (for re-attach), and starts the periodic title refresh timer.
        After this initial capture, all content comes via %output.
        """
        self._send_initial_resize()
        # Defer initial capture until after first ratio reconciliation
        # so VTEs are at the correct size. Only fires once.
        self._initial_capture_pending = True
        self._refresh_pane_titles()
        self._refresh_tab_labels()
        self._title_timer = GLib.timeout_add(3000, self._periodic_title_refresh)

    def _send_initial_captures(self):
        """One-shot capture of existing pane content for re-attach.

        Called after the first ratio reconciliation so VTEs are at their
        correct size. Never called again — subsequent content arrives
        via the %output stream.
        """
        dbg('sending initial capture-pane commands')
        for window_id, tree in self._layout_trees.items():
            for pane_id in get_pane_ids(tree):
                self.protocol.send_command(
                    'capture-pane -J -p -t {} -e -S - -E -'.format(
                        pane_id),
                    callback=lambda result, pid=pane_id:
                        self._feed_initial_capture(pid, result),
                )

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

    def _chars_to_max_pixels(self, cols, rows):
        """Convert character dimensions to max pixel size for geometry hints.

        Accounts for layout structure, chrome, and CSD."""
        terminals = list(self.controller.terminal_to_pane.keys())
        if not terminals:
            return None

        term = None
        char_w = char_h = sb_w = tb_h = vpad_x = vpad_y = 0
        for t in terminals:
            char_w, char_h, sb_w, tb_h, vpad_x, vpad_y = \
                self._get_terminal_metrics(t)
            if char_w > 0:
                term = t
                break
        if not term or char_w <= 0:
            return None

        handle_size = self._get_handle_size(term)

        # Use the active window's tree for structural info
        tree = None
        awid = self.controller.active_window_id
        if awid and awid in self._layout_trees:
            tree = self._layout_trees[awid]
        if tree is None:
            for t in self._layout_trees.values():
                if self._is_active_window(t):
                    tree = t
                    break
        if tree is None:
            for tree in self._layout_trees.values():
                break
        if tree is None:
            return None

        target_w = self._subtree_px(tree, 'h', char_w, char_h, sb_w, tb_h,
                                     handle_size, vpad_x, vpad_y)
        target_h = self._subtree_px(tree, 'v', char_w, char_h, sb_w, tb_h,
                                     handle_size, vpad_x, vpad_y)

        # Scale from tree's char dimensions to the requested dimensions
        if tree.width > 0 and tree.height > 0:
            target_w = target_w * cols / tree.width
            target_h = target_h * rows / tree.height

        window = term.get_toplevel()

        # Chrome = tab bar, borders — NOT other panes.
        chrome_w, chrome_h = self._get_chrome_size(window)

        max_w = int(target_w) + chrome_w
        max_h = int(target_h) + chrome_h

        return (max_w, max_h, chrome_w, chrome_h, 0, 0)

    def _set_max_size_pixels(self, max_w, max_h):
        """Apply max size geometry hints on the GTK window.

        max_w/max_h are content-based (get_size() coordinate space).
        Geometry hints constrain the outer allocation (including CSD),
        so we add CSD to reach the intended content max.
        Returns (hint_w, hint_h) — the actual values set on the hint
        (in allocation space, i.e. content + CSD).
        """
        terminals = list(self.controller.terminal_to_pane.keys())
        if not terminals:
            return (max_w, max_h)
        window = terminals[0].get_toplevel()
        # CSD = difference between outer allocation and content size
        alloc = window.get_allocation()
        ws = window.get_size()
        csd_w = max(0, alloc.width - ws[0])
        csd_h = max(0, alloc.height - ws[1])
        hint_w = max_w + csd_w
        hint_h = max_h + csd_h
        dbg('size_trace set_max_pixels: '
            'content_max=%dx%d csd=%dx%d '
            'hint=%dx%d alloc=%dx%d ws=%dx%d' % (
            max_w, max_h, csd_w, csd_h,
            hint_w, hint_h,
            alloc.width, alloc.height, ws[0], ws[1]))
        # Store in allocation space so window.py re-applies correctly
        # as geometry hints (which constrain allocation, not content)
        window._tmux_max_size = (hint_w, hint_h)
        # If CSD not yet known (window just created), skip —
        # setting hints without CSD makes the max too tight.
        # The tripwire will set correct hints once CSD is available.
        if csd_w == 0 and csd_h == 0:
            return (max_w, max_h)
        from gi.repository import Gdk
        geometry = Gdk.Geometry()
        geometry.max_width = hint_w
        geometry.max_height = hint_h
        window.set_geometry_hints(None, geometry, Gdk.WindowHints.MAX_SIZE)
        return (hint_w, hint_h)

    def _update_max_from_tree(self, cols, rows):
        """Update per-axis max size constraints from layout tree dimensions.

        Each axis is tracked independently: a column limit doesn't
        prevent the user from adding rows, and vice versa.
        """
        if self.controller._last_client_size is None:
            dbg('size_trace update_max: skipped (initial startup)')
            return False

        sent = self.controller._last_client_size
        max_c = self.controller._tmux_max_cols
        max_r = self.controller._tmux_max_rows

        # Per-axis: did tmux exceed a known limit (grew) or reject?
        grew_c = max_c is not None and cols > max_c
        grew_r = max_r is not None and rows > max_r
        rej_c = sent and cols < sent[0]
        rej_r = sent and rows < sent[1]

        if grew_c or grew_r:
            # Exceeded a known limit — clear that axis's constraint
            if grew_c:
                self.controller._tmux_max_cols = None
            if grew_r:
                self.controller._tmux_max_rows = None
            dbg('size_trace update_max: grew cols=%s->%d rows=%s->%d' % (
                max_c or 'free', cols, max_r or 'free', rows))

        if rej_c or rej_r:
            # At least one axis was rejected — constrain only that axis.
            if rej_c:
                self.controller._tmux_max_cols = cols
            if rej_r:
                self.controller._tmux_max_rows = rows
            dbg('size_trace update_max: rejected '
                'sent=%dx%d got=%dx%d max_cols=%s max_rows=%s' % (
                sent[0], sent[1], cols, rows,
                self.controller._tmux_max_cols or 'free',
                self.controller._tmux_max_rows or 'free'))
            # Use constraint values for pixel computation, not tree values.
            # The tree may be smaller than the limit (e.g. 118 cols when
            # max is 132) — we need MAX at the limit, not current size.
            hint_cols = self.controller._tmux_max_cols \
                if self.controller._tmux_max_cols is not None else cols
            hint_rows = self.controller._tmux_max_rows \
                if self.controller._tmux_max_rows is not None else rows
            info = self._chars_to_max_pixels(hint_cols, hint_rows)
            if info:
                # Use accumulated state, not just this cycle's rejection,
                # so existing constraints on the other axis are preserved.
                max_w = info[0] if self.controller._tmux_max_cols is not None else 32767
                max_h = info[1] if self.controller._tmux_max_rows is not None else 32767
                self._set_max_size_pixels(max_w, max_h)
            self.controller._arm_tripwire_after_idle()
        elif grew_c or grew_r:
            # Grew past a limit — arm tripwire instantly
            self.controller._do_arm_tripwire()
        else:
            # Echo-back confirmation, no change
            if not self.controller._tripwire_armed \
                    and not self.controller._tripwire_timer:
                self.controller._do_arm_tripwire()
        return False

    def _clear_tmux_max_size(self):
        """Remove the max size constraint."""
        terminals = list(self.controller.terminal_to_pane.keys())
        if not terminals:
            return False

        window = terminals[0].get_toplevel()
        if getattr(window, '_tmux_max_size', None):
            dbg('clearing tmux max size constraint')
            window._tmux_max_size = None
            window.set_geometry_hints(None, None, 0)

        return False

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
        # Chrome = tab bar, borders — NOT other panes.
        # window.resize() operates in content space — no CSD.
        chrome_w, chrome_h = self._get_chrome_size(window)

        win_w = int(target_w) + chrome_w
        win_h = int(target_h) + chrome_h

        wa = window.get_allocation()
        ws = window.get_size()
        dbg('size_trace resize_to_tree: '
            'alloc=%dx%d ws=%dx%d '
            'chrome=%dx%d resize=%dx%d tree=%dx%d' % (
            wa.width, wa.height, ws[0], ws[1],
            chrome_w, chrome_h, win_w, win_h,
            tree.width, tree.height))

        window.resize(win_w, win_h)
        # Update pixel tracking so notify_resize detects this as a
        # window resize (not a split drag) when VTE sizes change
        self.controller._last_window_pixels = (win_w, win_h)
        self.controller._layout_applied_time = time.monotonic()
        # If window isn't already at target size, the WM hasn't
        # processed the resize yet.  Gate _finish_applying_layout
        # on configure-event so we don't clear _applying_layout
        # while paneds are still at old size.
        if ws != (win_w, win_h):
            self.controller._window_resize_pending = True
            self.controller._ensure_configure_handler(window)
        return False

    def _send_initial_resize(self):
        """Size our window to match tmux's layout, then tell tmux our size.

        Uses actual VTE char metrics + scrollbar/titlebar/handle sizes to
        compute the correct pixel dimensions. If the layout won't fit on
        screen, clamps to screen size and tells tmux to use the smaller
        dimensions.
        """
        # Use the active window's tree — other windows may have
        # been pre-shrunk by tmux.  Prefer the window ID from
        # tmux's active flag (available before terminals are
        # mapped), then fall back to _is_active_window (checks
        # which tab is visible).
        tree = None
        awid = self.controller.active_window_id
        if awid and awid in self._layout_trees:
            tree = self._layout_trees[awid]
        if tree is None:
            for t in self._layout_trees.values():
                if self._is_active_window(t):
                    tree = t
                    break
        if tree is None:
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
            dbg('initial resize to %dx%d (from tmux layout, '
                     'no metrics yet)' % (tree.width, tree.height))
            self.protocol.send_command(
                'refresh-client -C {},{}'.format(tree.width, tree.height))
            return

        handle_size = self._get_handle_size(term)

        # Measure VTE CSS padding
        ctx = term.vte.get_style_context()
        vte_css_pad = ctx.get_padding(ctx.get_state())
        dbg('initial_resize step1: metrics '
            'char=%dx%d sb=%d tb=%d handle=%d vpad=%dx%d '
            'vte_css_pad=l%d,r%d,t%d,b%d' % (
            char_w, char_h, sb_w, tb_h,
            handle_size, vpad_x, vpad_y,
            vte_css_pad.left, vte_css_pad.right,
            vte_css_pad.top, vte_css_pad.bottom))

        # Compute pixel dimensions for tmux's layout (content area)
        target_w = self._subtree_px(tree, 'h', char_w, char_h,
                                     sb_w, tb_h, handle_size,
                                     vpad_x, vpad_y)
        target_h = self._subtree_px(tree, 'v', char_w, char_h,
                                     sb_w, tb_h, handle_size,
                                     vpad_x, vpad_y)

        dbg('initial_resize step2: subtree_px '
            'tmux=%dx%d target=%.1fx%.1f' % (
            tree.width, tree.height, target_w, target_h))

        # Measure every layer of the window
        window = term.get_toplevel()
        wa = window.get_allocation()
        ws = window.get_size()
        content = window.get_child()
        ca = content.get_allocation() if content else None
        ta = term.get_allocation()
        vte_alloc = term.vte.get_allocation()
        vc = term.vte.get_column_count()
        vr = term.vte.get_row_count()

        dbg('initial_resize step3: layers '
            'win_alloc=%dx%d win_size=%dx%d '
            'content=%s term=%dx%d '
            'vte=%dx%d vte_chars=%dx%d' % (
            wa.width, wa.height, ws[0], ws[1],
            '%dx%d' % (ca.width, ca.height)
                if ca else 'None',
            ta.width, ta.height,
            vte_alloc.width, vte_alloc.height,
            vc, vr))

        # Chrome using different reference points
        csd_w = wa.width - ws[0]
        csd_h = wa.height - ws[1]
        chrome_ws_vte = (ws[0] - vte_alloc.width,
                         ws[1] - vte_alloc.height)
        chrome_alloc_vte = (wa.width - vte_alloc.width,
                            wa.height - vte_alloc.height)
        chrome_content_term = (
            (ca.width - ta.width, ca.height - ta.height)
            if ca else (0, 0))

        dbg('initial_resize step4: chrome options '
            'csd=%dx%d '
            'ws-vte=%dx%d '
            'alloc-vte=%dx%d '
            'content-term=%dx%d' % (
            csd_w, csd_h,
            chrome_ws_vte[0], chrome_ws_vte[1],
            chrome_alloc_vte[0], chrome_alloc_vte[1],
            chrome_content_term[0], chrome_content_term[1]))

        # Chrome = tab bar, borders — NOT other panes.
        # window.resize() operates in content space (get_size()),
        # NOT allocation space — do NOT add CSD.
        chrome_w, chrome_h = self._get_chrome_size(window)

        # Get screen limits
        from gi.repository import Gdk
        screen = window.get_screen()
        monitor = screen.get_monitor_at_window(window.get_window()) \
            if window.get_window() else 0
        mon_geom = screen.get_monitor_workarea(monitor)
        max_w = mon_geom.width
        max_h = mon_geom.height

        target_win_w = int(target_w) + chrome_w
        target_win_h = int(target_h) + chrome_h

        fits = target_win_w <= max_w and target_win_h <= max_h
        win_w = min(target_win_w, max_w)
        win_h = min(target_win_h, max_h)

        dbg('initial_resize step5: final '
            'target_win=%dx%d resize=%dx%d '
            'screen=%dx%d fits=%s' % (
            target_win_w, target_win_h,
            win_w, win_h,
            max_w, max_h, fits))

        ws_before = window.get_size()
        window.resize(win_w, win_h)

        # Gate _finish_applying_layout on the WM's response
        if ws_before != (win_w, win_h):
            self.controller._window_resize_pending = True
            self.controller._ensure_configure_handler(window)

        # Set initial chrome baseline for change detection
        self.controller._last_chrome = self._get_chrome_size(window)

        # Check what happened after resize
        wa2 = window.get_allocation()
        ws2 = window.get_size()
        va2 = term.vte.get_allocation()
        vc2 = term.vte.get_column_count()
        vr2 = term.vte.get_row_count()
        dbg('initial_resize step6: after resize '
            'win_alloc=%dx%d win_size=%dx%d '
            'vte=%dx%d vte_chars=%dx%d' % (
            wa2.width, wa2.height, ws2[0], ws2[1],
            va2.width, va2.height, vc2, vr2))

        if fits:
            # Tell tmux we match its layout
            dbg('initial resize to %dx%d (matches tmux)' % (
                tree.width, tree.height))
            self.protocol.send_command(
                'refresh-client -C {},{}'.format(tree.width, tree.height))
        else:
            # Screen too small — compute what we can fit and tell tmux
            # to downsize. Back-calculate character dimensions from pixels.
            fit_cols = (win_w - vpad_x - sb_w) // char_w
            fit_rows = (win_h - vpad_y - tb_h) // char_h
            dbg('initial resize to %dx%d (screen limited from %dx%d)' % (
                fit_cols, fit_rows, tree.width, tree.height))
            self.protocol.send_command(
                'refresh-client -C {},{}'.format(fit_cols, fit_rows))

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
        """Query tmux for current window names and active pane info."""
        self.protocol.send_command(
            'list-windows -F "#{window_id}\t#{window_index}\t#{window_name}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_title}"',
            callback=self._on_window_names,
        )

    def _on_window_names(self, result):
        """Handle window name query response and update tab labels."""
        if result.is_error:
            return
        import os, socket
        home = os.environ.get('HOME', '')
        hostname = socket.gethostname()
        for line in result.output_lines:
            decoded = line.decode('utf-8', errors='replace').strip()
            parts = decoded.split('\t', 5)
            if len(parts) < 6:
                continue
            window_id, index, name, command, path, pane_title = parts
            self.controller.window_names[window_id] = name
            self.controller.window_indices[window_id] = index
            # Build tab label: command:path or custom title (no size)
            if home and path.startswith(home):
                path = '~' + path[len(home):]
            has_custom = (pane_title and pane_title != hostname
                          and pane_title != command)
            if has_custom:
                tab_label = pane_title
            else:
                tab_label = '%s:%s' % (command, path) if path else command
            GLib.idle_add(self._update_tab_label, window_id, tab_label)
        # Set the window title to hostname: session-name
        if self.controller.session_name:
            self.protocol.send_command(
                'display-message -p "#{user}@#{host_short}"',
                callback=self._on_tmux_hostname,
            )

    def _on_tmux_hostname(self, result):
        """Handle hostname query response."""
        userhost = 'tmux'
        if not result.is_error and result.output_lines:
            userhost = result.output_lines[0].decode('utf-8', errors='replace').strip()
        title = '%s: %s [tmux]' % (userhost, self.controller.session_name)
        GLib.idle_add(self._set_window_title, title)

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
        import os, socket
        home = os.environ.get('HOME', '')
        hostname = socket.gethostname()
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
            # Detect app-set custom title (not tmux's default hostname)
            has_custom = (pane_title and pane_title != hostname
                          and pane_title != command)
            if has_custom:
                title = pane_title
            else:
                title = '%s:%s' % (command, path) if path else command
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
