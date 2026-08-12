"""Count alone is ambiguous, and the monitor must be unable to be built that way.

Half as many integrations in a day is EITHER less work arriving OR a box that got twice as slow,
and one series cannot tell those apart. So every assertion below is stated as a RATIO between two
operating points -- never as the value at one, which is the reference-value defect wearing a
throughput hat.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent_swarm import throughput


_DAY = date(2026, 8, 12)


def _window(count: int, gate_seconds: float, *, days: int = 1) -> throughput.Window:
    monitor = throughput.Throughput()
    for _ in range(count):
        monitor.record(_DAY, gate_seconds=gate_seconds)
    return monitor.window(days=days)


class TestBothSeriesAreMandatory:
    def test_an_integration_without_its_gate_duration_is_refused(self):
        """The single-series monitor, refused at the door rather than mis-read later."""
        monitor = throughput.Throughput()
        with pytest.raises(throughput.UnmeasuredIntegration):
            monitor.record(_DAY, gate_seconds=0.0)
        with pytest.raises(throughput.UnmeasuredIntegration):
            monitor.record(_DAY, gate_seconds=-1.0)

    def test_a_window_over_no_integrations_refuses_to_state_a_ceiling(self):
        """No durations means no ceiling. Reporting 0.0 saturation would read as "plenty of room"
        on exactly the evidence that cannot support it."""
        empty = throughput.Throughput().window(days=1)
        assert empty.integrations == 0
        with pytest.raises(throughput.UnmeasuredIntegration):
            _ = empty.ceiling_per_day


class TestTheCalibrationIsREPRODUCED:
    """704 s per gate, whole box -- the measurement the formula must land on, checked rather than
    stored. A constant would be a number anyone could drift; this is the arithmetic itself."""

    def test_704_seconds_gives_about_five_per_hour(self):
        assert _window(1, 704.0).per_hour == pytest.approx(5.11, abs=0.01)

    def test_and_at_most_forty_per_day(self):
        window = _window(1, 704.0)
        assert window.ceiling_per_day <= 41.0
        assert window.ceiling_per_day >= 40.0

    def test_the_observed_demand_is_a_quarter_of_the_ceiling(self):
        """~10/day against ~40/day. The QUANTITY THAT MATTERS is this ratio, not either term."""
        assert _window(10, 704.0).saturation == pytest.approx(0.25, abs=0.02)


class TestTheRatioIsWhatMoves:
    def test_halving_the_gate_doubles_the_ceiling(self):
        fast, slow = _window(1, 352.0), _window(1, 704.0)
        assert fast.ceiling_per_day / slow.ceiling_per_day == pytest.approx(2.0)

    def test_the_same_count_on_a_slower_box_is_nearer_the_ceiling(self):
        """The discriminating assertion. Count is IDENTICAL in both windows; saturation is not."""
        quick, sluggish = _window(10, 704.0), _window(10, 1408.0)
        assert quick.integrations == sluggish.integrations
        assert sluggish.saturation / quick.saturation == pytest.approx(2.0)

    def test_every_input_to_the_ceiling_is_readable(self):
        """A computed property whose inputs are hidden is a number a reader must trust."""
        inputs = _window(10, 704.0).inputs
        assert inputs['mean_gate_seconds'] == pytest.approx(704.0)
        assert inputs['integrations'] == 10
        assert inputs['available_hours_per_day'] == throughput.AVAILABLE_HOURS_PER_DAY
        assert inputs['days'] == 1


class TestTheDiagnosisCountAloneCannotMake:
    def test_fewer_integrations_at_the_same_speed_is_falling_demand(self):
        change = throughput.compare(_window(20, 704.0), _window(10, 704.0))
        assert change.count_ratio == pytest.approx(0.5)
        assert change.duration_ratio == pytest.approx(1.0)
        assert change.reading == throughput.DEMAND_FELL

    def test_fewer_integrations_on_a_box_that_slowed_is_NOT(self):
        """Same count series as above; the duration series is what separates them, and a monitor
        that recorded only the first would report DEMAND_FELL for both."""
        change = throughput.compare(_window(20, 704.0), _window(10, 1408.0))
        assert change.count_ratio == pytest.approx(0.5)
        assert change.duration_ratio == pytest.approx(2.0)
        assert change.reading == throughput.THE_BOX_SLOWED

    def test_more_work_at_the_same_speed_is_rising_demand(self):
        change = throughput.compare(_window(10, 704.0), _window(20, 704.0))
        assert change.reading == throughput.DEMAND_ROSE

    def test_an_unchanged_fleet_says_so(self):
        assert throughput.compare(_window(10, 704.0), _window(10, 704.0)).reading == throughput.UNCHANGED

    def test_the_saturation_ratio_is_carried_because_it_is_the_ceiling_question(self):
        change = throughput.compare(_window(20, 704.0), _window(10, 1408.0))
        assert change.saturation_ratio == pytest.approx(1.0), 'half the work at twice the cost is the same load'
