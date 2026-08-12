"""`clock.run_cli` is the operator's four knobs, and each one must reach the field it names.

WHY THIS MOVED HERE. Every flag below sets a parameter of `Clock.run`, so the flag is this module's
vocabulary and not any consumer's -- and the FIRST consumer that wrote its own parser demonstrated
the cost: alongside its four `add_argument` calls it re-ran `lifetime.bind_children_to_this_process`
immediately before `run`, which binds too. One guarantee, two implementations, each docstring
describing the other's. The parser is here now so a second consumer cannot grow a second `--wake-port`
that feeds nothing.

THE DISCRIMINATING ASSERTION is `test_every_flag_reaches_the_field_it_names`. A CLI that parsed all
four and passed none of them on would exit 0 on every invocation, print nothing wrong, and silently
run at the policy's cadence with a listener nobody asked for -- the failure shape that looks like
success. Asserting the parse alone would pass against exactly that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm import clock

pytestmark = pytest.mark.unit


class _RecordingClock:
    """Stands in for a built `Clock`, recording the kwargs `run_cli` hands to `run`."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def run(self, **kwargs) -> int:
        self.kwargs = kwargs
        return 0


@pytest.fixture
def built():
    recorder = _RecordingClock()
    return recorder, lambda: recorder


def test_every_flag_reaches_the_field_it_names(built) -> None:
    recorder, build = built
    assert (
        clock.run_cli(build, description='x', argv=['--interval', '7', '--wake-host', '::1', '--wake-port', '0']) == 0
    )
    assert recorder.kwargs == {'interval': 7.0, 'once': False, 'wake_host': '::1', 'wake_port': 0}


def test_the_defaults_are_the_modules_own_and_an_absent_interval_stays_absent(built) -> None:
    """`interval=None` is not a missing value: it is the instruction to read the policy file, and a
    CLI that substituted a number here would pin the cadence of every consumer to argparse.
    """
    recorder, build = built
    clock.run_cli(build, description='x', argv=[])
    assert recorder.kwargs == {
        'interval': None,
        'once': False,
        'wake_host': clock.WAKE_HOST,
        'wake_port': clock.WAKE_PORT,
    }


def test_once_is_forwarded_because_it_is_the_only_way_the_loop_ever_returns(built) -> None:
    recorder, build = built
    clock.run_cli(build, description='x', argv=['--once'])
    assert recorder.kwargs is not None and recorder.kwargs['once'] is True


def test_the_clock_is_BUILT_only_after_the_arguments_parse(tmp_path: Path) -> None:
    """`build` is a callable so `--help` and a bad flag cost nothing, and so a construction refusal
    -- `Clock.__post_init__` rejects an empty tick command -- surfaces after parsing rather than
    before it, where the message would be about the wrong mistake.
    """
    built = []

    def _build():
        built.append(1)
        return _RecordingClock()

    with pytest.raises(SystemExit):
        clock.run_cli(_build, description='x', argv=['--no-such-flag'])
    assert built == []


def test_the_description_has_no_default_so_this_package_never_speaks_for_a_consumer() -> None:
    """A default here would put this package's prose on the `--help` of a program it knows nothing
    else about -- the same defect as a defaulted tick command, one layer up.
    """
    with pytest.raises(TypeError):
        clock.run_cli(lambda: _RecordingClock(), argv=[])  # type: ignore[call-arg]
