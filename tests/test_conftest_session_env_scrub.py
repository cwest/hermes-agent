"""The hermetic env fixture must scrub every session-context variable.

``gateway.session_context.get_session_env`` falls back to ``os.environ`` when
its ContextVar is unset, which is the state every unit test starts in. Any
``HERMES_SESSION_*`` / cron auto-deliver var left in the developer's shell
(a Hermes tool subprocess carries ``HERMES_SESSION_ID`` bridged from its turn)
therefore leaks into tests and silently changes behaviour. The kanban origin
mirror guard keys off ``HERMES_SESSION_ID``: with it leaked, a test modelling a
detached worker is judged as a per-turn context and the inherited mirror is
rejected.

``tests/conftest.py`` keeps its own name list rather than importing
``gateway.session_context`` at collection time, so this guard pins the two in
agreement.
"""

import os

from gateway.session_context import _VAR_MAP, get_session_env
from tests.conftest import _HERMES_BEHAVIORAL_VARS


def test_every_session_context_var_is_scrubbed():
    missing = sorted(set(_VAR_MAP) - _HERMES_BEHAVIORAL_VARS)
    assert not missing, (
        "tests/conftest.py::_HERMES_BEHAVIORAL_VARS is missing session-context "
        f"vars from gateway.session_context._VAR_MAP: {missing}"
    )


def test_session_id_is_not_visible_inside_a_test():
    assert "HERMES_SESSION_ID" not in os.environ
    assert get_session_env("HERMES_SESSION_ID", "") == ""
