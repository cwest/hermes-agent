"""The kanban origin must SURVIVE the subprocess-env bridge (inheritance).

Complement to ``test_local_env_session_leak.py``: that suite proves the
``HERMES_SESSION_*`` identity vars are STRIPPED across the bridge when unset in
an engaged process (leak guard). This suite proves the opposite contract for
``HERMES_KANBAN_ORIGIN`` — it must be CARRIED across the bridge so a detached
child (dispatched worker / delegate_task / background process) inherits the
human origin of the workstream that spawned it.

The origin is deliberately NOT a ``_VAR_MAP`` member, so it is not subject to the
engaged-strip. It rides its ``os.environ`` mirror, which the bridge preserves.
"""

import json
import os

import pytest

import gateway.session_context as sc
from gateway.session_context import _VAR_MAP, set_kanban_origin
from tools.environments.local import (
    _make_run_env,
    _sanitize_subprocess_env,
    hermes_subprocess_env,
)

_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"
SESSION_VARS = list(_VAR_MAP.keys())


@pytest.fixture(autouse=True)
def _isolate():
    saved_env = {k: os.environ.get(k) for k in SESSION_VARS}
    saved_origin = os.environ.get(_ORIGIN_ENV)
    saved_ctx = {name: var.get() for name, var in _VAR_MAP.items()}
    saved_engaged = sc._session_context_engaged
    saved_origin_ctx = sc._KANBAN_ORIGIN.get()
    for var in _VAR_MAP.values():
        var.set(sc._UNSET)
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ.pop(_ORIGIN_ENV, None)
    sc._session_context_engaged = False
    try:
        yield
    finally:
        for var, val in zip(_VAR_MAP.values(), saved_ctx.values()):
            var.set(val)
        sc._KANBAN_ORIGIN.set(saved_origin_ctx)
        sc._session_context_engaged = saved_engaged
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_origin is None:
            os.environ.pop(_ORIGIN_ENV, None)
        else:
            os.environ[_ORIGIN_ENV] = saved_origin


def _engage():
    sc._session_context_engaged = True


def _assert_origin(env, chat_id):
    blob = env.get(_ORIGIN_ENV)
    assert blob, f"{_ORIGIN_ENV} was not carried into the child env"
    assert json.loads(blob)["chat_id"] == chat_id


# --------------------------------------------------------------------------- #
# Foreground path (_make_run_env)
# --------------------------------------------------------------------------- #

def test_origin_carried_across_bridge_when_engaged_and_session_detached():
    """The core inheritance case: engaged host, DETACHED session, origin set.

    A dispatched worker's session identity is foreign/unset (so the identity
    vars strip), but the inherited kanban origin must ride through to the child.
    """
    _engage()
    set_kanban_origin(platform="discord", chat_id="HUMAN_ORIGIN", thread_id="HT")
    env = _make_run_env({})
    _assert_origin(env, "HUMAN_ORIGIN")


def test_origin_carried_via_os_environ_mirror_when_contextvar_unset():
    """A grandchild process (ContextVar _UNSET, only the mirror set) still carries it."""
    _engage()
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ[_ORIGIN_ENV] = json.dumps(
        {"platform": "discord", "chat_id": "GRANDCHILD", "thread_id": "GT"}
    )
    env = _make_run_env({})
    _assert_origin(env, "GRANDCHILD")


def test_no_origin_leaves_var_absent():
    """No origin bound anywhere → the child env carries no origin var."""
    _engage()
    env = _make_run_env({})
    assert _ORIGIN_ENV not in env


# --------------------------------------------------------------------------- #
# Background / PTY path (_sanitize_subprocess_env)
# --------------------------------------------------------------------------- #

def test_origin_carried_across_background_bridge():
    _engage()
    set_kanban_origin(platform="discord", chat_id="BG_ORIGIN", thread_id="BT")
    base = {"PATH": "/usr/bin:/bin", _ORIGIN_ENV: os.environ[_ORIGIN_ENV]}
    sanitized = _sanitize_subprocess_env(base)
    _assert_origin(sanitized, "BG_ORIGIN")


# --------------------------------------------------------------------------- #
# Non-terminal spawn surface (hermes_subprocess_env)
# --------------------------------------------------------------------------- #

def test_origin_carried_across_hermes_subprocess_env():
    _engage()
    set_kanban_origin(platform="discord", chat_id="SPAWN_ORIGIN", thread_id="ST")
    env = hermes_subprocess_env()
    _assert_origin(env, "SPAWN_ORIGIN")
