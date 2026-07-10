from dtdc_simulator.engine.mv import ManipulatedVariable, Mode


def test_manual_ignores_auto_writes() -> None:
    mv = ManipulatedVariable(
        key="x", manual_setpoint=1.0, auto_setpoint=1.0, mode=Mode.MANUAL, min=0, max=10
    )
    mv.set_auto_setpoint(9.0)
    assert mv.tick(0.1) == 1.0


def test_auto_ignores_manual_writes() -> None:
    mv = ManipulatedVariable(
        key="x", manual_setpoint=1.0, auto_setpoint=2.0, mode=Mode.AUTO, min=0, max=10
    )
    mv.set_manual_setpoint(9.0)
    assert mv.tick(0.1) == 2.0


def test_bumpless_transfer_manual_to_auto() -> None:
    mv = ManipulatedVariable(
        key="x", manual_setpoint=5.0, auto_setpoint=1.0, mode=Mode.MANUAL, min=0, max=10
    )
    mv.tick(0.1)
    before = mv.effective_value
    mv.set_mode(Mode.AUTO)
    assert mv.auto_setpoint == before
    after = mv.tick(0.1)
    assert after == before  # no step at the instant of switch


def test_bumpless_transfer_auto_to_manual() -> None:
    mv = ManipulatedVariable(
        key="x", manual_setpoint=1.0, auto_setpoint=7.0, mode=Mode.AUTO, min=0, max=10
    )
    mv.tick(0.1)
    before = mv.effective_value
    mv.set_mode(Mode.MANUAL)
    assert mv.manual_setpoint == before
    after = mv.tick(0.1)
    assert after == before


def test_clamping_to_limits() -> None:
    mv = ManipulatedVariable(
        key="x", manual_setpoint=100.0, auto_setpoint=0.0, mode=Mode.MANUAL, min=0, max=10
    )
    assert mv.tick(0.1) == 10.0


def test_rate_limit_caps_step() -> None:
    mv = ManipulatedVariable(
        key="x",
        manual_setpoint=0.0,
        auto_setpoint=0.0,
        mode=Mode.MANUAL,
        min=-100,
        max=100,
        rate_limit=1.0,
    )
    mv.set_manual_setpoint(100.0)
    v1 = mv.tick(1.0)
    assert v1 == 1.0  # rate_limit=1 unit/s, dt=1s
    v2 = mv.tick(1.0)
    assert v2 == 2.0
