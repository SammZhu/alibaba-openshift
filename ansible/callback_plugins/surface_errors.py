# Aggregate callback: on any task/item failure, print a compact, high-signal
# error block (stderr / module_stderr / msg / stdout tail / rc) so the real
# cause is visible without scrolling the result JSON.
#
# Runs ALONGSIDE the default stdout callback (CALLBACK_TYPE=aggregate), so it
# does not change normal output — it only adds an error block on failure.
#
# Limitation: ansible sanitises no_log task results BEFORE callbacks run, so for
# a no_log task this can only note "censored" — the real error must be surfaced
# by the task itself (see tasks/run_checked.yml + the create/update_stack pattern).
#
# Enable in ansible.cfg:
#   [defaults]
#   callback_plugins  = ./callback_plugins
#   callbacks_enabled = surface_errors
from __future__ import annotations
from ansible.plugins.callback import CallbackBase

DOCUMENTATION = '''
  name: surface_errors
  type: aggregate
  short_description: Print a clean error block when a task fails
  version_added: "2.11"
  description:
    - On task/item failure, prints the most useful error fields in a bordered
      block so the real cause is visible without reading the result JSON.
  requirements:
    - whitelist in configuration (callbacks_enabled = surface_errors)
'''

_BAR = "═" * 64


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "surface_errors"
    CALLBACK_NEEDS_ENABLED = True

    def _line(self, s):
        self._display.display("  " + s, color="bright red")

    def _emit(self, label, host, res):
        if not isinstance(res, dict):
            return
        self._display.display("\n╔═ ERROR · %s · %s %s" % (label, host, _BAR[:20]),
                              color="bright red")
        # no_log tasks arrive censored — point at the task's own surfacing step.
        if res.get("_ansible_no_log") or res.get("censored"):
            self._line("output is no_log-censored — the task's surfacing step (or "
                       "tasks/run_checked.yml) prints the real error.")
            self._display.display("╚" + _BAR, color="bright red")
            return

        emitted = False

        def put(prefix, val, tail=None):
            nonlocal emitted
            if val is None:
                return
            items = val if isinstance(val, list) else str(val).splitlines()
            items = [i for i in items if str(i).strip()]
            if tail:
                items = items[-tail:]
            for i in items:
                self._line("%s%s" % (prefix, i))
                emitted = True

        msg = res.get("msg")
        if msg and msg not in ("All items completed", "MODULE FAILURE"):
            put("", msg)
        put("stderr: ", res.get("stderr_lines") or res.get("stderr"), tail=25)
        if not res.get("stderr_lines") and not res.get("stderr"):
            put("stderr: ", res.get("module_stderr"), tail=25)
        # some CLIs print the error to stdout
        put("stdout: ", res.get("stdout_lines") or res.get("stdout"), tail=8)
        if res.get("rc") is not None:
            self._line("rc: %s" % res.get("rc"))
            emitted = True
        if not emitted:
            self._line("(no extra detail beyond the default output above)")
        self._display.display("╚" + _BAR, color="bright red")

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if ignore_errors:
            return
        self._emit(result._task.get_name(), result._host.get_name(), result._result)

    def v2_runner_item_on_failed(self, result):
        self._emit(result._task.get_name() + " (item)", result._host.get_name(), result._result)

    def v2_runner_on_async_failed(self, result):
        self._emit(result._task.get_name() + " (async)", result._host.get_name(), result._result)
