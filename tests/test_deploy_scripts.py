"""Deploy-script protocol pins (REVIEW_SYSTEM_0c30c77 findings 6/7/15).

PowerShell cannot execute on this dev/CI platform (the cmdlets —
Stop-ScheduledTask, Get-ScheduledTask — are Windows-only and pwsh is not
installed), so these are structural tests: they pin the verified-dead stop
protocol's load-bearing elements in the script text, so a regression back
to assume-dead semantics ("the file vanished, print stopped") fails
loudly here instead of silently lying to an operator. The executed
verification of the protocol on the target box is ARRIVAL_CHECKLIST §9.
"""

from __future__ import annotations

import re
from pathlib import Path

from palletscan.reliability.instance_lock import LOCK_BYTE_OFFSET

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _stop_script() -> str:
    return (_DEPLOY / "stop_palletscan.ps1").read_text(encoding="utf-8")


def _start_script() -> str:
    return (_DEPLOY / "start_palletscan.ps1").read_text(encoding="utf-8")


def test_stop_script_never_claims_task_stop_kills_the_tree() -> None:
    """Finding 7: the old script asserted 'Stop-ScheduledTask
    hard-terminates the whole tree' as fact and relied on it — it kills
    only the supervisor. Tree-kill now holds only via the (best-effort)
    job object, and the script must verify, not assume."""
    text = _stop_script()
    assert "hard-terminates the whole tree" not in text
    # The job object is described as best-effort, with the explicit
    # child-kill step still present.
    assert "best-effort" in text
    assert "Stop-Process" in text


def test_stop_script_verifies_death_via_lock_probes() -> None:
    """Finding 7 + design-review fix: liveness is probed by a NON-BLOCKING
    LOCK ATTEMPT on the same byte the holder locks (lock-vs-lock conflict
    is the documented semantic; a read past EOF is not), and success
    messages are verified-dead only."""
    text = _stop_script()
    offset = re.search(r"\$LockByteOffset\s*=\s*(0x[0-9A-Fa-f]+)", text)
    assert offset is not None, "the script must pin the lock byte offset"
    assert int(offset.group(1), 16) == LOCK_BYTE_OFFSET
    assert ".Lock($LockByteOffset, 1)" in text
    assert "ReadByte" not in text, "read-probes past EOF have unproven semantics"
    # Every success message states the verification basis.
    for line in text.splitlines():
        if "stopped" in line.lower() and "Write-Host" in line:
            assert "verified" in line or "not running" in line, line


def test_stop_script_probes_both_locks_and_kills_the_holder_pid() -> None:
    """Finding 6: an orphaned writer (a child the dead supervisor's state
    knows nothing about) is detected through palletscan.lock itself and
    killed by the pid in the lock's diagnostics JSON."""
    text = _stop_script()
    assert "palletscan.lock" in text
    assert "palletscan.supervisor.lock" in text
    assert "Get-LockHolderPid" in text
    assert "ConvertFrom-Json" in text  # holder pid from the lock's JSON line


def test_stop_script_polls_after_kill_and_refuses_wrong_data_dir() -> None:
    """Design-review fixes: lock release after an abnormal kill is
    indeterminate (poll, never single-shot), and a bare invocation must
    not act on a directory that never hosted a station (the old silent
    default-dir mismatch made every graceful stop escalate)."""
    text = _stop_script()
    assert "Wait-LocksFree" in text
    assert "Get-StationDataDir" in text
    assert "no PalletScan station state found" in text
    assert "exit 2" in text


def test_stop_script_never_removes_the_stop_latch() -> None:
    """Findings 13/15: the stop-file is a sticky latch — only an explicit
    start removes it. The stop script must not Remove-Item it (the old
    script did, as part of its escalation)."""
    assert "Remove-Item" not in _stop_script()


def test_start_script_clears_the_latch_and_verifies_the_start() -> None:
    """Findings 13/15 + design-review fix: the explicit start is the one
    sanctioned discard of a stop request, it must target the task's real
    data dir (derived, not defaulted), and success is verified against the
    task state."""
    text = _start_script()
    assert "Remove-Item" in text and "supervisor.stop" in text
    assert "Get-StationDataDir" in text
    assert "Start-ScheduledTask" in text
    # the removal happens before the start
    assert text.index("Remove-Item") < text.index("Start-ScheduledTask")
    assert '"Running"' in text
    assert "did not reach the Running state" in text
