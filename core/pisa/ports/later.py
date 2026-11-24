"""A collaborator that exists only after the service calling it: how the runtime closes a cycle in its wiring.

Two services that call each other (the approvals apply a plan option, and the plan's options write through the
approvals) cannot both be built first. The one built first takes a `Later` at construction and calls it only
at call time; the runtime fills it once, right after building the other. Calling it before then is a wiring
bug and says so, never a silent None."""


class Later[T]:
    def __init__(self, what: str) -> None:
        self._what = what
        self._value: T | None = None

    def fill(self, value: T) -> None:
        if self._value is not None:
            raise RuntimeError(f"{self._what} is wired once")
        self._value = value

    def __call__(self) -> T:
        if self._value is None:
            raise RuntimeError(f"{self._what} is not wired yet")
        return self._value
