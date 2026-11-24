"""What Pisa said to each device without being asked ("newest wins"): a notify or
a push (a check-in included) or an approval question, in the order Pisa said them. One API process delivers
them all (the outbox dispatcher, the approvals), so a sequence in memory orders them against the turns."""

import itertools


class Unasked:
    def __init__(self) -> None:
        self._seq = itertools.count(1)
        self._last: dict[object, int] = {}
        self._what: dict[object, str | None] = {}  # what the newest one was: a check-in's id, else None

    def mark(self) -> int:
        """Now, in the sequence: a turn takes it when Pisa has finished asking."""
        return next(self._seq)

    def said(self, device_id: object, check_in: str | None = None) -> None:
        """Pisa said something to this device unasked (on its socket or by push); `check_in`: its id, if
        one."""
        self._last[device_id] = next(self._seq)
        self._what[device_id] = check_in

    def newest_check_in(self, device_id: object) -> str | None:
        """The check-in Pisa said last to this device unasked, when it is the newest thing said unasked (else
        None: a reminder, an approval or nothing known since the process started)."""
        return self._what.get(device_id)

    def since(self, device_id: object, mark: int) -> bool:
        return self._last.get(device_id, 0) > mark
