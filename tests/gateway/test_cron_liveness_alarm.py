"""Out-of-band cron-scheduler liveness alarm (gateway housekeeping thread).

The cron scheduler runs in its own daemon thread; when that thread dies (e.g. a
corrupt executions.db that escaped recovery, or any unhandled error), every
scheduled job freezes while the gateway process otherwise looks healthy. The
original alerting lived ON the cron that died, so nothing fired.

These tests cover the independent watchdog: the gateway's own housekeeping
thread (separate from the cron scheduler thread) periodically checks the cron
ticker heartbeat age and posts a ONE-SHOT alarm to the home channel when it
goes stale, re-arming only after it recovers. The decision logic is a pure
function so it is tested without spinning the real thread.
"""
from __future__ import annotations


def test_alarm_fires_when_heartbeat_is_stale():
    from gateway.run import _evaluate_cron_liveness_alarm

    decision = _evaluate_cron_liveness_alarm(
        heartbeat_age=600.0,          # 10 min — well past threshold
        threshold_seconds=300.0,
        already_alarmed=False,
    )
    assert decision.should_alarm is True
    assert decision.next_already_alarmed is True
    assert "cron" in decision.message.lower()


def test_alarm_does_not_refire_while_still_stale():
    """Once alarmed, a still-stale heartbeat must not spam the home channel."""
    from gateway.run import _evaluate_cron_liveness_alarm

    decision = _evaluate_cron_liveness_alarm(
        heartbeat_age=900.0,
        threshold_seconds=300.0,
        already_alarmed=True,
    )
    assert decision.should_alarm is False
    assert decision.next_already_alarmed is True


def test_alarm_rearms_after_recovery():
    """A fresh heartbeat clears the alarmed latch so a future stall re-fires."""
    from gateway.run import _evaluate_cron_liveness_alarm

    decision = _evaluate_cron_liveness_alarm(
        heartbeat_age=5.0,            # healthy
        threshold_seconds=300.0,
        already_alarmed=True,
    )
    assert decision.should_alarm is False
    assert decision.next_already_alarmed is False


def test_unknown_heartbeat_age_does_not_alarm():
    """heartbeat_age is None when the marker is missing/unreadable (older build,
    never ran, torn read). Unknown must be treated as 'cannot determine', NOT a
    stall — the watchdog must not false-alarm on a gateway that simply never
    started cron."""
    from gateway.run import _evaluate_cron_liveness_alarm

    decision = _evaluate_cron_liveness_alarm(
        heartbeat_age=None,
        threshold_seconds=300.0,
        already_alarmed=False,
    )
    assert decision.should_alarm is False
    assert decision.next_already_alarmed is False


def test_healthy_heartbeat_does_not_alarm():
    from gateway.run import _evaluate_cron_liveness_alarm

    decision = _evaluate_cron_liveness_alarm(
        heartbeat_age=30.0,
        threshold_seconds=300.0,
        already_alarmed=False,
    )
    assert decision.should_alarm is False
    assert decision.next_already_alarmed is False
