"""Session states, their labels, and the timing rules around them."""


DELIVERY_ACTIVITY = {None: "sending prompt from web", "compact": "compacting conversation",
                     "clear": "clearing conversation"}
COMMAND_TIMEOUT = 600    # /clear, /compact: their receipt is SessionStart, sent only once they finish

INPUT_STATES = ("idle", "error")  # states in which we will type into the session

LABELS = {"working": "working", "needs_input": "waiting on a terminal dialog", "starting": "starting",
          "unknown": "in an unknown state"}
STATE_RANK = {"needs_input": 0, "error": 1, "unknown": 2, "working": 3, "starting": 4, "idle": 5, "exited": 6}
