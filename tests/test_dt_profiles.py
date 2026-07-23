from types import SimpleNamespace

from dtdc_simulator.interfaces.ui.dt_profiles import _profile_status_text


def test_profile_status_distinguishes_accepted_and_rejected_attempts() -> None:
    accepted = SimpleNamespace(
        dt_solver_converged=True,
        dt_last_solve_sim_time=80.0,
        dt_last_attempt_sim_time=80.0,
    )
    rejected = SimpleNamespace(
        dt_solver_converged=False,
        dt_last_solve_sim_time=80.0,
        dt_last_attempt_sim_time=95.0,
    )

    assert _profile_status_text(accepted, 100.0) == (
        "(accepted profile resolved 20s of sim time ago)"
    )
    assert _profile_status_text(rejected, 100.0) == (
        "(latest solve rejected 5s ago; showing last accepted profile from 20s ago)"
    )
