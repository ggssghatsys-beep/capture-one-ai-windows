"""Synthetic restart boundary tests: no process/window/application is touched."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from c1ai import native_lifecycle as lifecycle
from c1ai import source_snapshot, tangent_transport, windows_backend


class NativeFunction:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    target = {"instance": {"pid": 100, "started_at": "123", "version": "16.8.test"},
              "document": {"path": str(tmp_path / "session.cosessiondb")},
              "image": {"path": str(tmp_path / "source.NEF")},
              "variant": {"id": "original-variant"}, "layer": "Background"}
    observation = {"target": target, "raw_state": {"window": {
        "hwnd": 555, "pid": 100, "exe": str(tmp_path / "CaptureOne.exe")}}}
    after = deepcopy(observation)
    after["target"]["instance"].update(pid=200, started_at="456")
    state = SimpleNamespace(observation=observation, after=after, clock=0, close_messages=[],
        wait_result=0, post_result=True, handle=777, closed_handles=[], launches=[], actions=[],
        guard_error=None, observe_error=None, launch_error=None, source_changed=False,
        process_start="123", window_pid=100, queries=[], snapshots=0, worker_closes=0,
        first_status={"status": "ok", "listening": True, "connected": False},
        last_status={"status": "ok", "listening": True, "connected": True,
                     "stream_synchronized": True,
                     "instance": {"pid": 200, "started_at": "456", "executable": str(tmp_path / "CaptureOne.exe")}})

    def window_pid(hwnd, output):
        output._obj.value = state.window_pid
        return 1

    def post(hwnd, message, wparam, lparam):
        state.close_messages.append((hwnd, message, wparam, lparam))
        return state.post_result

    kernel = SimpleNamespace(OpenProcess=NativeFunction(lambda *args: state.handle),
        WaitForSingleObject=NativeFunction(lambda handle, timeout: state.wait_result),
        CloseHandle=NativeFunction(lambda handle: state.closed_handles.append(handle)))
    user = SimpleNamespace(PostMessageW=NativeFunction(post),
        GetWindowThreadProcessId=NativeFunction(window_pid),
        IsWindow=NativeFunction(lambda hwnd: bool(state.window_pid)))
    monkeypatch.setattr(lifecycle.ctypes, "WinDLL", lambda name, **kwargs: kernel if name == "kernel32" else user)
    monkeypatch.setattr(windows_backend, "process_started", lambda pid: state.process_start)

    def snapshot(target):
        state.snapshots += 1
        return {"source_sha256": "raw", "adjustment_sha256":
                "changed" if state.source_changed and state.snapshots > 1 else "saved"}

    def status(request, **kwargs):
        state.queries.append(deepcopy(request))
        return deepcopy(state.first_status if len(state.queries) == 1 else state.last_status)

    def launch(argv, **kwargs):
        state.launches.append((argv, kwargs))
        if state.launch_error:
            raise state.launch_error
        return SimpleNamespace(pid=200)

    def sleep(seconds):
        state.clock += seconds

    monkeypatch.setattr(source_snapshot, "saved_source", snapshot)
    monkeypatch.setattr(tangent_transport, "call", status)
    monkeypatch.setattr(lifecycle.subprocess, "Popen", launch)
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: state.clock)
    monkeypatch.setattr(lifecycle.time, "sleep", sleep)
    monkeypatch.setattr(lifecycle, "_assert_no_capture_one_process", lambda executable: None)
    monkeypatch.setattr(lifecycle, "_saved_tangent_enabled", lambda target: {'explicit_enabled': True, 'scope': 'synthetic_setting'})

    class Backend:
        window = observation["raw_state"]["window"]

        def __init__(self):
            self.worker = SimpleNamespace(close=self.close_worker)

        def close_worker(self):
            state.worker_closes += 1

        def guard(self, observed):
            assert observed == observation
            return {"identity": "original"}

        def call(self, command, **kwargs):
            assert command == "guard"
            if state.guard_error:
                raise state.guard_error
            return {"verified": True}

        def action(self, name, function, intent):
            state.actions.append({"name": name, "action_id": str(len(state.actions)), "intent": deepcopy(intent), "status": "dispatching"})
            try:
                result = function()
            except Exception as error:
                state.actions[-1].update(status="not_dispatched" if getattr(error, "dispatched", None) is False else "outcome_unknown")
                raise
            state.actions[-1].update(status="completed", result=result)
            return result

        def observe(self, fields):
            assert fields == []
            if state.observe_error:
                raise state.observe_error
            return deepcopy(state.after)

    state.backend = Backend()
    state.run = lambda: lifecycle.restart_for_native_transport(state.backend, state.observation)
    return state


def test_exact_exit_and_saved_source_precede_single_launch(rig):
    result = rig.run()
    assert result["status"] == "completed"
    assert result["saved_source_unchanged"] is True
    assert rig.close_messages == [(555, 0x10, 0, 0)]
    assert rig.closed_handles == [777]
    assert len(rig.launches) == 1 and rig.launches[0][0] == [
        rig.observation["raw_state"]["window"]["exe"], rig.observation["target"]["document"]["path"]]
    assert [a["name"] for a in rig.actions] == ["restart.close", "restart.launch"]
    assert rig.actions[0]["intent"]["saved_source"]["adjustment_sha256"] == "saved"


@pytest.mark.parametrize("status", [{"status": "refused"}, {"status": "ok", "listening": False}])
def test_unready_endpoint_never_closes_or_launches(rig, status):
    rig.first_status = status
    with pytest.raises(windows_backend.NativeError, match="native transport"):
        rig.run()
    assert not rig.close_messages and not rig.launches and not rig.actions


def test_changed_process_prevents_close(rig):
    rig.process_start = "different-start"
    with pytest.raises(windows_backend.NativeError, match="changed before"):
        rig.run()
    assert not rig.close_messages and not rig.launches
    assert rig.actions[0]["status"] == "not_dispatched"
    assert rig.closed_handles == [777]


def test_guard_failure_prevents_close(rig):
    rig.guard_error = windows_backend.NativeError("TARGET_CHANGED", "synthetic guard failure")
    with pytest.raises(windows_backend.NativeError, match="guard failure"):
        rig.run()
    assert not rig.close_messages and not rig.launches


def test_post_failure_is_not_dispatched_and_does_not_launch(rig):
    rig.post_result = False
    with pytest.raises(windows_backend.NativeError, match="not sent"):
        rig.run()
    assert len(rig.close_messages) == 1 and not rig.launches
    assert rig.actions[0]["status"] == "not_dispatched"


@pytest.mark.parametrize("wait_result", [258, 0xFFFFFFFF])
def test_non_exited_or_failed_wait_never_repeats_close_or_launch(rig, wait_result):
    rig.wait_result = wait_result
    with pytest.raises(windows_backend.NativeError, match="not exited"):
        rig.run()
    assert len(rig.close_messages) == 1 and not rig.launches
    assert rig.actions[0]["status"] == "outcome_unknown"
    assert rig.closed_handles == [777]


def test_saved_adjustment_change_stops_after_close_before_launch(rig):
    rig.source_changed = True
    with pytest.raises(windows_backend.NativeError, match="Saved source changed"):
        rig.run()
    assert len(rig.close_messages) == 1 and not rig.launches and rig.worker_closes == 0


def test_startup_timeout_never_relaunches(rig):
    rig.observe_error = RuntimeError("startup incomplete")
    with pytest.raises(windows_backend.NativeError, match="Relaunch sent once"):
        rig.run()
    assert len(rig.launches) == 1 and len(rig.close_messages) == 1


@pytest.mark.parametrize("key,value", [("variant", {"id": "different"}), ("layer", "Adjustment 1"),
                                       ("instance", {"pid": 100, "started_at": "123", "version": "16.8.test"})])
def test_new_instance_requires_original_selected_target(rig, key, value):
    rig.after["target"][key] = value
    with pytest.raises(windows_backend.NativeError, match="exact selected target"):
        rig.run()
    assert len(rig.launches) == 1


def test_failed_popen_is_not_retried(rig):
    rig.launch_error = OSError("synthetic launch error")
    with pytest.raises(OSError, match="launch error"):
        rig.run()
    assert len(rig.launches) == 1 and len(rig.close_messages) == 1


def test_recycled_hwnd_is_never_closed(rig):
    rig.window_pid = 999
    with pytest.raises(windows_backend.NativeError):
        rig.run()
    assert not rig.close_messages and not rig.launches


@pytest.mark.parametrize("change", [{"connected": False}, {"stream_synchronized": False},
                                    {"instance": {"pid": 999, "started_at": "456"}},
                                    {"instance": {"pid": 200, "started_at": "old"}}])
def test_restart_does_not_claim_transport_ready_without_exact_new_peer(rig, change):
    rig.last_status.update(change)
    result = rig.run()
    assert result['status'] == 'completed' and result['scope'] == 'application_restart_completed'
    assert result['native_transport_ready'] is False
    assert len(rig.close_messages) == 1 and len(rig.launches) == 1


def restart_record(rig, *, launch_status="completed"):
    target = deepcopy(rig.observation["target"])
    record = {"operation_id": "restart-operation", "target": target, "before": deepcopy(rig.observation),
              "request": {"command": "images", "action": "restart_for_native_transport"},
              "actions": [{"action_id": "close-action", "name": "restart.close", "status": "completed",
                           "intent": {"original_target": target, "native_message": "WM_CLOSE",
                                      "saved_source": {"source_sha256": "raw", "adjustment_sha256": "saved"}},
                           "result": {"accepted": True, "normal_close_requested": True,
                                      "process_exit_verified": True, "forced_termination": False}}]}
    if launch_status is not None:
        record["actions"].append({"action_id": "launch-action", "name": "restart.launch", "status": launch_status,
            "intent": {"original_target": target, "executable": rig.observation["raw_state"]["window"]["exe"],
                       "session": target["document"]["path"]}, "result": {"accepted": True}})
    return record


def test_healthy_existing_link_is_no_action_success(rig):
    rig.first_status = deepcopy(rig.last_status)
    rig.first_status["instance"].update(pid=100, started_at="123")
    rig.after = deepcopy(rig.observation)
    result = rig.run()
    assert result["already_ready"] is True and result["restart_count"] == 0
    assert not rig.actions and not rig.launches and not rig.close_messages and rig.snapshots == 0


def test_healthy_link_still_checks_current_photo(rig):
    rig.first_status = deepcopy(rig.last_status)
    rig.first_status["instance"].update(pid=100, started_at="123")
    with pytest.raises(windows_backend.NativeError, match="Selected target changed"):
        rig.run()
    assert not rig.actions and not rig.launches and not rig.close_messages


@pytest.mark.parametrize("change", [{"pid": 999, "started_at": "123"},
                                    {"pid": 100, "started_at": "old"},
                                    {"pid": 100, "started_at": "123", "desynchronized": True}])
def test_occupied_or_desynchronized_transport_never_closes_current_application(rig, change):
    rig.first_status = deepcopy(rig.last_status)
    rig.first_status["instance"].update(pid=change["pid"], started_at=change["started_at"])
    if change.get("desynchronized"):
        rig.first_status["stream_synchronized"] = False
    with pytest.raises(windows_backend.NativeError, match="do not restart"):
        rig.run()
    assert not rig.actions and not rig.close_messages and not rig.launches


@pytest.mark.parametrize("status", ["completed", "dispatching", "outcome_unknown"])
def test_reconcile_late_start_is_read_only_and_retains_original_receipts(rig, status):
    record = restart_record(rig, launch_status=status)
    original = deepcopy(record)
    rig.first_status = deepcopy(rig.last_status)
    result = lifecycle.reconcile_restart(rig.backend, record)
    assert result["outcome"] == "applied_not_restored"
    assert result["evidence"]["scope"] == "application_restart_completed"
    assert result["evidence"]["image_adjustments_dispatched"] is False
    assert result["evidence"]["launch_action_id"] == "launch-action"
    assert result["target"]["instance"]["pid"] == 200
    assert record == original and not rig.actions and not rig.launches and not rig.close_messages


@pytest.mark.parametrize("status", [None, "not_dispatched"])
def test_reconcile_without_possible_original_launch_remains_unknown(rig, status):
    result = lifecycle.reconcile_restart(rig.backend, restart_record(rig, launch_status=status))
    assert result["outcome"] == "unknown"
    assert not rig.actions and not rig.launches and not rig.close_messages


@pytest.mark.parametrize("change", ["source", "target", "transport", "old_alive", "exit_receipt", "executable", "launch_intent"])
def test_reconcile_requires_every_original_and_current_guard(rig, change):
    record = restart_record(rig)
    rig.first_status = deepcopy(rig.last_status)
    if change == "source":
        rig.source_changed = True
        rig.snapshots = 1
    elif change == "target":
        rig.after["target"]["variant"]["id"] = "other"
    elif change == "transport":
        rig.first_status["instance"]["started_at"] = "other"
    elif change == "old_alive":
        rig.wait_result = 258
    elif change == "exit_receipt":
        record["actions"][0]["result"]["process_exit_verified"] = False
    elif change == "executable":
        rig.after["raw_state"]["window"]["exe"] += ".other"
    else:
        record["actions"][1]["intent"]["session"] += ".other"
    result = lifecycle.reconcile_restart(rig.backend, record)
    if change == 'transport':
        assert result['outcome'] == 'applied_not_restored' and result['evidence']['native_transport_ready'] is False
    else:
        assert result["outcome"] == "unknown" and result["evidence"]["error"]
    assert not rig.actions and not rig.launches and not rig.close_messages


def test_recovery_continues_only_missing_launch_then_repeated_query_does_not_relaunch(rig):
    record = restart_record(rig, launch_status=None)
    result = lifecycle.recover_restart(rig.backend, record)
    assert result["outcome"] == "applied_not_restored"
    assert result["evidence"]["continued_original_operation"] is True
    assert len(rig.launches) == 1 and not rig.close_messages
    assert [action["name"] for action in rig.actions] == ["restart.launch"]
    record["actions"].extend(deepcopy(rig.actions))
    again = lifecycle.recover_restart(rig.backend, record)
    assert again["outcome"] == "applied_not_restored"
    assert len(rig.launches) == 1 and not rig.close_messages


@pytest.mark.parametrize("status", ["completed", "dispatching", "outcome_unknown", "not_dispatched"])
def test_any_existing_launch_record_makes_recovery_read_only(rig, status):
    rig.first_status = deepcopy(rig.last_status)
    lifecycle.recover_restart(rig.backend, restart_record(rig, launch_status=status))
    assert not rig.launches and not rig.close_messages and not rig.actions


@pytest.mark.parametrize("change", ["old_alive", "source", "close_unknown", "endpoint", "already_connected", "manual_process"])
def test_recovery_never_launches_when_preconditions_fail(rig, change, monkeypatch):
    record = restart_record(rig, launch_status=None)
    if change == "old_alive":
        rig.wait_result = 258
    elif change == "source":
        rig.source_changed = True
        rig.snapshots = 1
    elif change == "close_unknown":
        record["actions"][0]["status"] = "outcome_unknown"
    elif change == "endpoint":
        rig.first_status["listening"] = False
    elif change == "already_connected":
        rig.first_status["connected"] = True
    else:
        def already_running(executable):
            raise windows_backend.NativeError("C1_ALREADY_RUNNING", "synthetic manually started instance")
        monkeypatch.setattr(lifecycle, "_assert_no_capture_one_process", already_running)
    with pytest.raises(windows_backend.NativeError):
        lifecycle.recover_restart(rig.backend, record)
    assert not rig.launches and not rig.close_messages
    if change == "manual_process":
        assert rig.actions[-1]["status"] == "not_dispatched"


def test_process_inventory_detects_c1_without_window_or_transport(monkeypatch):
    # Exercise the actual helper; do not enumerate the user's real processes.
    calls = []
    names = iter(["unrelated.exe", "CaptureOne.exe"])

    def next_process(handle, pointer):
        try:
            pointer._obj.szExeFile = next(names)
            return True
        except StopIteration:
            return False

    kernel = SimpleNamespace(CreateToolhelp32Snapshot=NativeFunction(lambda *args: 900),
        Process32FirstW=NativeFunction(next_process), Process32NextW=NativeFunction(next_process),
        CloseHandle=NativeFunction(lambda handle: calls.append(handle)))
    monkeypatch.setattr(lifecycle.ctypes, "WinDLL", lambda *args, **kwargs: kernel)
    monkeypatch.setattr(lifecycle.ctypes, "get_last_error", lambda: 18)
    with pytest.raises(windows_backend.NativeError, match="already exists"):
        lifecycle._assert_no_capture_one_process("C:/CaptureOne.exe")
    assert calls == [900]


@pytest.fixture
def empty_selection(rig, monkeypatch):
    candidate_id = '019c7efe-d1b6-4a4a-a326-f417db26f316'
    rig.observation['target']['variant'] = {'id': candidate_id, 'label': 'source.NEF [2]'}
    rig.observation['evidence'] = {'identity': {'database_variant_id': 926}}
    rig.observation['raw_state']['values'] = {'selection': {'name': 'source.NEF [2]'}}
    rig.after = deepcopy(rig.observation)
    rig.after['target']['instance'].update(pid=200, started_at='456')
    rig.after['raw_state']['window'].update(pid=200, hwnd=666, title='session', version='16.8.test', minimized=False)
    state = SimpleNamespace(record=restart_record(rig), selected=False, selection_calls=0,
                            fail_after_select=False, db_reads=0, change_db_on_second=False,
                            rows=[{'id': '11111111-1111-4111-8111-111111111111', 'index': 0, 'browser_variant_id': 925},
                                  {'id': candidate_id, 'index': 1, 'browser_variant_id': 926}])
    node = {'name': 'Variant: source.NEF - VariantID: 926', 'control_type': 'DataItem',
            'patterns': ['SelectionItem'], 'selected': False, 'enabled': True, 'offscreen': False,
            'runtime_id': [42, 926]}
    node['selector'] = {'name': node['name'], 'control_type': 'DataItem'}
    state.nodes = [node]
    state.windows = [deepcopy(rig.after['raw_state']['window'])]
    rig.backend.config = {'session_path': rig.observation['target']['document']['path']}
    rig.first_status = deepcopy(rig.last_status)
    monkeypatch.setattr(windows_backend, 'process_started', lambda pid: '123' if pid == 100 else '456')

    def inventory(observation):
        assert observation == state.record['before']
        state.db_reads += 1
        rows = deepcopy(state.rows)
        if state.change_db_on_second and state.db_reads >= 2:
            rows[-1]['browser_variant_id'] = 999
        return rows

    def observe(fields):
        assert fields == []
        if not state.selected or state.fail_after_select:
            raise windows_backend.NativeError('CONTROL_NOT_FOUND', 'No selected viewer title')
        return deepcopy(rig.after)

    def native(command, **kwargs):
        if command == 'windows':
            return {'windows': deepcopy(state.windows)}
        window = deepcopy(state.windows[0])
        if command == 'read':
            assert kwargs['selector']['automation_id'] == 'SummaryText'
            return {'window': window, 'node': {'name': f'{int(state.selected)} 个，共 1 个'}}
        if command == 'inspect':
            return {'window': window, 'nodes': deepcopy(state.nodes), 'truncated': False, 'read_errors': []}
        if command == 'invoke':
            assert kwargs['mode'] == 'select'
            assert kwargs['selector']['name'].endswith('VariantID: 926')
            assert kwargs['guard']['expected']['empty_selection']['name'] == '0 个，共 1 个'
            assert kwargs['guard']['expected']['original_candidate']['selected'] is False
            state.selection_calls += 1
            state.selected = True
            return {'window': window, 'accepted': True, 'verified': True}
        raise AssertionError(command)

    rig.backend.variant_inventory = inventory
    rig.backend.observe = observe
    rig.backend.call = native
    state.rig = rig
    return state


def test_empty_selection_recovers_exact_original_uuid_once_without_launch(empty_selection):
    state, rig = empty_selection, empty_selection.rig
    result = lifecycle.recover_restart(rig.backend, state.record)
    assert result['outcome'] == 'applied_not_restored'
    assert result['target']['variant']['id'] == state.record['target']['variant']['id']
    assert result['evidence']['selection_recovery']['selection_verified'] is True
    assert [a['name'] for a in rig.actions] == ['restart.select_original']
    assert state.selection_calls == 1 and not rig.launches and not rig.close_messages
    state.record['actions'].extend(deepcopy(rig.actions))
    lifecycle.recover_restart(rig.backend, state.record)
    assert state.selection_calls == 1


@pytest.mark.parametrize('change', ['missing_pattern', 'missing_selected', 'duplicate_browser', 'missing_db_uuid',
                                   'changed_db_id', 'wrong_filename', 'offscreen', 'other_session', 'other_instance'])
def test_empty_selection_preflight_fails_closed(empty_selection, change):
    state, rig = empty_selection, empty_selection.rig
    if change == 'missing_pattern':
        state.nodes[0]['patterns'] = []
    elif change == 'missing_selected':
        state.nodes[0].pop('selected')
    elif change == 'duplicate_browser':
        state.nodes.append(deepcopy(state.nodes[0]))
    elif change == 'missing_db_uuid':
        state.rows[-1]['id'] = '22222222-2222-4222-8222-222222222222'
    elif change == 'changed_db_id':
        state.rows[-1]['browser_variant_id'] = 999
    elif change == 'wrong_filename':
        state.nodes[0]['name'] = state.nodes[0]['selector']['name'] = 'Variant: other.NEF - VariantID: 926'
    elif change == 'offscreen':
        state.nodes[0]['offscreen'] = True
    elif change == 'other_session':
        rig.backend.config['session_path'] += '.other'
    else:
        state.windows[0].update(pid=100)
    with pytest.raises((windows_backend.NativeError, RuntimeError)):
        lifecycle.recover_restart(rig.backend, state.record)
    assert state.selection_calls == 0 and not rig.actions and not rig.launches and not rig.close_messages


def test_empty_selection_rechecks_database_inside_journal_boundary(empty_selection):
    state, rig = empty_selection, empty_selection.rig
    state.change_db_on_second = True
    with pytest.raises(windows_backend.NativeError, match='browser ID'):
        lifecycle.recover_restart(rig.backend, state.record)
    assert len(rig.actions) == 1 and rig.actions[0]['status'] == 'not_dispatched'
    assert state.selection_calls == 0 and not rig.launches and not rig.close_messages


def test_missing_selection_readback_is_unknown_and_selection_never_repeats(empty_selection):
    state, rig = empty_selection, empty_selection.rig
    state.fail_after_select = True
    with pytest.raises(windows_backend.NativeError, match='viewer title'):
        lifecycle.recover_restart(rig.backend, state.record)
    assert rig.actions[0]['status'] == 'outcome_unknown' and state.selection_calls == 1
    state.record['actions'].extend(deepcopy(rig.actions))
    result = lifecycle.recover_restart(rig.backend, state.record)
    assert result['outcome'] == 'unknown' and state.selection_calls == 1


def test_existing_different_user_selection_is_not_replaced(empty_selection):
    state, rig = empty_selection, empty_selection.rig
    state.selected = True
    rig.after['target']['variant']['id'] = 'user-chosen-different-variant'
    result = lifecycle.recover_restart(rig.backend, state.record)
    assert result['outcome'] == 'unknown'
    assert not rig.actions and state.selection_calls == 0


def test_selection_can_complete_while_transport_is_still_unresolved(empty_selection):
    state, rig = empty_selection, empty_selection.rig
    rig.first_status['connected'] = False
    result = lifecycle.recover_restart(rig.backend, state.record)
    assert result['outcome'] == 'applied_not_restored'
    assert result['evidence']['native_transport_ready'] is False
    assert result['evidence']['selection_recovery']['selection_verified'] is True
    assert rig.actions[0]['status'] == 'completed' and state.selection_calls == 1


def test_unavailable_transport_status_does_not_erase_verified_restart(rig, monkeypatch):
    def unavailable(*args, **kwargs):
        raise TimeoutError('synthetic endpoint status timeout')
    monkeypatch.setattr(tangent_transport, 'call', unavailable)
    result = lifecycle.reconcile_restart(rig.backend, restart_record(rig))
    assert result['outcome'] == 'applied_not_restored'
    assert result['evidence']['native_transport_ready'] is None
    assert result['evidence']['native_transport']['error']['message'] == 'synthetic endpoint status timeout'
    assert not rig.actions and not rig.close_messages and not rig.launches


@pytest.mark.parametrize('name,status,result', [
    ('restart.recovery.enable_tangent', 'outcome_unknown', {}),
    ('restart.recovery.enable_tangent', 'dispatching', {}),
    ('restart.recovery.cancel_setup_notice', 'completed', {'accepted': True, 'notice_closed': False}),
    ('restart.recovery.open_preferences_exact', 'completed', {'accepted': True}),
    ('arbitrary.extra', 'completed', {'success': True, 'verified': True}),
])
def test_additional_dispatched_actions_cannot_be_erased_by_restart_reconciliation(rig, name, status, result):
    record = restart_record(rig)
    record['actions'].append({'action_id': 'extra', 'name': name, 'status': status, 'result': result})
    rig.first_status = deepcopy(rig.last_status)
    answer = lifecycle.reconcile_restart(rig.backend, record)
    assert answer['outcome'] == 'unknown'
    assert answer['evidence']['scope'] == 'restart_with_unreconciled_additional_actions'
    assert answer['evidence']['error']['code'] == 'RESTART_EXTRA_ACTIONS_UNVERIFIED'
    assert answer['evidence']['unresolved_extra_actions'][0]['action_id'] == 'extra'
    assert answer['evidence']['all_operation_effects_verified'] is False
    again = lifecycle.recover_restart(rig.backend, record)
    assert again['outcome'] == 'unknown'
    assert not rig.actions and not rig.launches and not rig.close_messages


def test_certified_not_dispatched_extra_does_not_block_restart_evidence(rig):
    record = restart_record(rig)
    record['actions'].append({'action_id': 'extra', 'name': 'restart.recovery.open_preferences', 'status': 'not_dispatched'})
    rig.first_status = deepcopy(rig.last_status)
    answer = lifecycle.reconcile_restart(rig.backend, record)
    assert answer['outcome'] == 'applied_not_restored'


@pytest.fixture
def known_setup(rig, monkeypatch):
    from c1ai import native_windows
    from c1ai.contracts import digest
    record = restart_record(rig)
    record['request_hash'] = digest(record['request'])
    record['actions'][1]['result']['launcher_pid'] = 200
    monkeypatch.setattr(lifecycle, '_SETUP_RECOVERY', {'operation_id': record['operation_id'],
                                                    'request_hash': record['request_hash']})
    rig.after['raw_state']['window'].update(pid=200, hwnd=666, title='session', version='16.8.test', started_at='456')
    rig.first_status = deepcopy(rig.last_status)
    menu = {'control_type': 'MenuItem', 'name': '编辑(E)'}
    preferences = {'control_type': 'MenuItem', 'name': '偏好设置…'}
    checkbox = {'control_type': 'CheckBox', 'name': '启用 Tangent 调色设备支持'}
    guard = {'bindings': {'menu': menu}, 'expected': {'menu': {'name': '编辑(E)'}}, 'window_title': 'session'}
    expanded = {'bindings': {'menu': menu}, 'expected': {'menu': {'expand_state': 'Expanded'}}, 'window_title': 'session'}
    notice = {'pid': 200, 'hwnd': 700, 'owner_hwnd': 666, 'parent_hwnd': 0, 'owner_chain': [666],
              'title': 'Capture One', 'class_name': 'HwndWrapper[CaptureOne;Main;example]',
              'enabled': True, 'visible': True, 'text_truncated': False, 'uia_modal_verified': False}
    intents = {
        'open_edit_menu': {'action': 'invoke', 'guard': guard, 'mode': 'expand', 'selector': menu},
        'edit_menu_foreground': {'action': 'invoke', 'guard': guard, 'mode': 'expand', 'selector': menu},
        'open_preferences': {'action': 'invoke', 'guard': expanded, 'selector': {**preferences, 'ancestors': [menu]}},
        'open_preferences_exact': {'action': 'invoke', 'guard': expanded, 'selector': preferences},
        'enable_tangent': {'action': 'set', 'guard': {'bindings': {'tangent': checkbox},
            'expected': {'tangent': {'toggle': 'Off'}}, 'window_title': '偏好设置'},
            'hwnd': 690, 'mode': 'toggle', 'selector': checkbox, 'value': True},
        'cancel_setup_notice': {'close_type': 'WM_CLOSE', 'instance': {'pid': 200, 'started_at': '456'}, 'notice': notice},
    }
    for name, intent in intents.items():
        record['actions'].append({'name': 'restart.recovery.' + name, 'action_id': name, 'intent': deepcopy(intent),
            'status': 'not_dispatched' if name == 'open_preferences' else 'outcome_unknown' if name == 'enable_tangent' else 'completed'})
    state = SimpleNamespace(record=record, rig=rig, modal=False, inventory_complete=True, enabled=True,
                            setting_reads=0, setting_changes=False, setting_enabled=True, menu='Collapsed', observe_calls=0)

    def inventory(*args, **kwargs):
        return {'enumeration_complete': state.inventory_complete, 'truncated': False, 'read_errors': [],
                'main_enabled': not state.modal,
                'windows': [{'hwnd': 700, 'visible': True, 'title': 'Capture One'}] if state.modal else []}

    def setting(target):
        state.setting_reads += 1
        if not state.setting_enabled:
            raise windows_backend.NativeError('TANGENT_DISABLED', 'Saved preference is not enabled')
        return {'explicit_enabled': True, 'path': 'synthetic-user.config',
                'sha256': 'changed' if state.setting_changes and state.setting_reads > 1 else 'stable',
                'scope': 'persisted_user_setting'}

    def observe(fields):
        state.observe_calls += 1
        return deepcopy(rig.after)

    def call(command, **kwargs):
        assert command in {'read', 'guard'}
        result = {'window': deepcopy(rig.after['raw_state']['window']), 'verified': True}
        if command == 'read':
            assert kwargs['selector'] == menu
            result['node'] = {'expand_state': state.menu}
        return result

    monkeypatch.setattr(native_windows, 'find_native_main', lambda *args: deepcopy(rig.after['raw_state']['window']))
    monkeypatch.setattr(native_windows, 'native_window_inventory', inventory)
    monkeypatch.setattr(lifecycle, '_saved_tangent_enabled', setting)
    rig.backend.observe, rig.backend.call, rig.backend.guard = observe, call, lambda observed: {'synthetic': True}
    return state


def test_reviewed_setup_can_converge_by_independent_readback_without_changing_history(known_setup):
    state = known_setup
    original = deepcopy(state.record)
    result = lifecycle.reconcile_restart(state.rig.backend, state.record)
    assert result['outcome'] == 'applied_not_restored'
    proof = result['evidence']['setup_reconciliation']
    assert proof['current_state_converged'] is True
    assert proof['persisted_tangent_enabled'] is True and proof['menus_and_setup_windows_closed'] is True
    assert proof['original_toggle_success_claimed'] is False and proof['runtime_connector_enabled_verified'] is False
    assert state.record == original and state.record['actions'][-2]['status'] == 'outcome_unknown'
    assert not state.rig.actions and not state.rig.launches and not state.rig.close_messages


@pytest.mark.parametrize('change', ['modal', 'inventory', 'disabled', 'setting_changed', 'menu', 'request_hash',
                                   'other_operation', 'extra_action', 'mode', 'selector', 'notice_type', 'instance'])
def test_setup_needs_exact_reviewed_intents_and_current_independent_proof(known_setup, change):
    state = known_setup
    if change == 'modal':
        state.modal = True
    elif change == 'inventory':
        state.inventory_complete = False
    elif change == 'disabled':
        state.setting_enabled = False
    elif change == 'setting_changed':
        state.setting_changes = True
    elif change == 'menu':
        state.menu = 'Expanded'
    elif change == 'request_hash':
        state.record['request_hash'] = 'other'
    elif change == 'other_operation':
        state.record['operation_id'] = 'other'
    elif change == 'extra_action':
        state.record['actions'].append({'name': 'unreviewed.setting', 'status': 'completed', 'action_id': 'new', 'intent': {}})
    elif change == 'mode':
        state.record['actions'][-2]['intent']['mode'] = 'number'
    elif change == 'selector':
        state.record['actions'][-2]['intent']['selector']['name'] = 'Other preference'
    elif change == 'notice_type':
        state.record['actions'][-1]['intent']['close_type'] = 'WM_QUIT'
    else:
        state.record['actions'][-1]['intent']['instance']['started_at'] = 'other'
    result = lifecycle.reconcile_restart(state.rig.backend, state.record)
    assert result['outcome'] == 'unknown'
    assert result['evidence']['unresolved_extra_actions']
    assert not state.rig.actions and not state.rig.launches and not state.rig.close_messages
    if change in {'modal', 'inventory'}:
        assert state.observe_calls == 0  # Never enter the blocked UIA provider.


def test_disabled_preference_prevents_any_close_or_launch(rig, monkeypatch):
    def disabled(target):
        raise windows_backend.NativeError('TANGENT_DISABLED', 'synthetic disabled setting')
    monkeypatch.setattr(lifecycle, '_saved_tangent_enabled', disabled)
    with pytest.raises(windows_backend.NativeError, match='disabled setting'):
        rig.run()
    assert not rig.actions and not rig.close_messages and not rig.launches
    rig.queries.clear()
    with pytest.raises(windows_backend.NativeError, match='disabled setting'):
        lifecycle.recover_restart(rig.backend, restart_record(rig, launch_status=None))
    assert not rig.actions and not rig.close_messages and not rig.launches


@pytest.mark.parametrize('value', [None, 'False', 'True'])
def test_persisted_tangent_preference_is_version_specific_and_explicit(tmp_path, monkeypatch, value):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    folder = tmp_path / 'Capture_One' / 'CaptureOne_StrongName_test' / '16.8.6.3784'
    folder.mkdir(parents=True)
    entry = '' if value is None else f'<setting name="TangentConnectorEnabled"><value>{value}</value></setting>'
    path = folder / 'user.config'
    path.write_text(f'<configuration><userSettings><P1.C1.ApplicationSettings>{entry}</P1.C1.ApplicationSettings></userSettings></configuration>')
    target = {'instance': {'version': '16.8.6.3784'}}
    if value == 'True':
        result = lifecycle._saved_tangent_enabled(target)
        assert result['explicit_enabled'] is True and result['path'] == str(path)
        assert len(result['sha256']) == 64
    else:
        with pytest.raises(windows_backend.NativeError, match='Enable and save'):
            lifecycle._saved_tangent_enabled(target)
    with pytest.raises(windows_backend.NativeError, match='version-specific'):
        lifecycle._saved_tangent_enabled({'instance': {'version': '16.8.1.3586'}})
