# Terminator tmux control mode support
import sys

def tmux_dbg(msg):
    """Print tmux debug message unconditionally to stderr."""
    print('[tmux] %s' % msg, file=sys.stderr)
