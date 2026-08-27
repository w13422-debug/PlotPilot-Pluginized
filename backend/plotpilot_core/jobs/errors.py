from __future__ import annotations


class ExecutionError(Exception):
    """Stable application error returned by the execution authority."""

    def __init__(self, code: int, name: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.name = name
        self.message = message


def stale_lease(message: str = "attempt lease is not current") -> ExecutionError:
    return ExecutionError(1002, "stale_lease", message)


def duplicate_request(message: str = "operation key payload drift") -> ExecutionError:
    return ExecutionError(1008, "duplicate_request", message)


def invalid_transition(message: str) -> ExecutionError:
    return ExecutionError(1010, "invalid_transition", message)


def result_contract_mismatch(message: str) -> ExecutionError:
    return ExecutionError(1011, "result_contract_mismatch", message)
