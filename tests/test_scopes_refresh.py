"""A changed frame must not receive the disconnected-source retry penalty."""
import pytest
pytest.importorskip('PySide6')
from c1ai.scopes_window import refresh_delay_ms
from c1ai.analysis_service import failure_details
from c1ai.windows_backend import NativeError


def test_dragging_retries_promptly_but_disconnect_backs_off():
    assert refresh_delay_ms({'freshness':{'state':'stale'},
        'error':'Selection or revision changed; late image discarded'})==50
    assert refresh_delay_ms({'freshness':{'state':'waiting'},
        'error':'Native renderer has not completed the observed revision'})==50
    assert refresh_delay_ms({'freshness':{'state':'stale'},'error':'Connection refused'})==1500
    assert refresh_delay_ms({'freshness':{'state':'ready'}})==100
    assert refresh_delay_ms({'freshness':{'state':'stale'},'error':'System.InvalidOperationException: native target changed'})==50
    assert refresh_delay_ms({'freshness':{'state':'stale'},'error':'main form not ready'})==200
    for message in ('native preview still rendering; poll its id before starting another',
                    'preview target or adjustments changed; render again'):
        for state in ('stale','failed'):
            assert refresh_delay_ms({'freshness':{'state':state},'error':message})==50


def test_wrapped_native_error_retains_retry_reason():
    error=NativeError('INTERNAL_MODEL_ERROR','C1 internal model call did not complete',
        {'error':{'message':'System.InvalidOperationException: preview target or adjustments changed; render again\n at stack'}})
    details=failure_details(error)
    assert details['retry_after_ms']==50
    assert refresh_delay_ms({'freshness':{'state':'stale'},**details})==50
    assert 'stack' not in details['error']
    assert 'retry_after_ms' not in failure_details(NativeError('INTERNAL_MODEL_UNAVAILABLE','Pipe disconnected'))
