from dtdc_simulator.engine.clock import FreeRunClock, RealTimeClock


def test_freerun_advance_is_dt_sim_and_no_sleep() -> None:
    clock = FreeRunClock(dt_sim=2.0)
    assert clock.advance(0.2) == 2.0
    clock.pace(0.2)  # must not block
    assert clock.actual_speed == float("inf")


def test_realtime_advance_scales_by_speed_factor() -> None:
    clock = RealTimeClock(speed_factor=3.0)
    assert clock.advance(0.5) == 1.5
