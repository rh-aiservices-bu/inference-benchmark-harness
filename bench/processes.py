"""Conservative process-group checks shared by both runners."""

import subprocess
import sys

from .provenance import timestamp


def nonrunning_darwin_group(process_id, observation=None):
    """Darwin may return EPERM for a group whose remaining members are zombies."""
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=2)
        if observation is not None:
            observation.update(command=["ps", "-axo", "pgid=,stat="], exit_code=result.returncode,
                               observed=timestamp())
        if result.returncode:
            return False
        states = [fields[1] for line in result.stdout.splitlines()
                  if len(fields := line.split()) == 2 and fields[0] == str(process_id)]
        if observation is not None:
            observation["target_group_states"] = states
        return all(state.startswith("Z") for state in states)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if observation is not None:
            observation.update(error=type(exc).__name__, observed=timestamp())
        return False

