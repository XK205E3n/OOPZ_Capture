import io
import json
import urllib.error
from uuid import uuid4

import pytest

from test_analysis_pipeline import make_session, RecordingClient
from oopz_capture.analysis_pipeline import run_analysis
from oopz_capture.deepseek_client import ContentInspectionError, AnalysisAPIError, urllib_transport


@pytest.mark.parametrize('error', [
    {'code': 'data_inspection_failed', 'message': 'private input'},
    {'message': 'Upstream failed: [data_inspection_failed] private input'},
])
def test_transport_classifies_content_error_without_leaking_body(monkeypatch, error):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError('https://example.test', 400, 'Bad Request', {},
            io.BytesIO(json.dumps({'error': error}).encode()))
    monkeypatch.setattr('urllib.request.urlopen', fail)
    with pytest.raises(ContentInspectionError) as caught:
        urllib_transport('https://example.test', {}, {}, 1)
    assert 'private input' not in str(caught.value)


@pytest.mark.parametrize('blocked', [set(), {2}, {3}, {2, 3}])
def test_split_once_skip_report_and_resume(tmp_path, blocked):
    handoff = make_session(tmp_path, short_window_count=1)
    session = handoff.parent.parent
    path = session / 'transcript.jsonl'
    first = json.loads(path.read_text(encoding='utf-8'))
    second = dict(first, segment_id=str(uuid4()), start_ms=151000, end_ms=152000, text='second half')
    path.write_text(json.dumps(first) + '\n' + json.dumps(second) + '\n', encoding='utf-8')
    request = json.loads(handoff.read_text(encoding='utf-8'))
    request['inputs']['segment_count'] = 2
    handoff.write_text(json.dumps(request), encoding='utf-8')
    original = path.read_bytes()

    class Client(RecordingClient):
        def complete_json(self, **kwargs):
            number = len(self.calls) + 1
            if number == 1 or number in blocked:
                self.calls.append(kwargs)
                raise ContentInspectionError()
            return super().complete_json(**kwargs)

    client = Client()
    output = run_analysis(handoff, client)
    result = output['result']
    short = result['short_summaries'][0]
    assert len(client.calls) == 5  # original, two halves, long, final
    assert len(short['skipped_intervals']) == len(blocked)
    assert [p['start_ms'] for p in short['analysis_parts']] == [0, 150000]
    assert [p['end_ms'] for p in short['analysis_parts']] == [150000, 300000]
    assert 'second half' not in client.calls[1]['user_prompt']
    assert 'second half' in client.calls[2]['user_prompt']
    assert result['model']['usage_by_stage']['short_summaries']['api_calls'] == 3
    for name in ['summary.md', 'summary.public.md', 'summary.text.md']:
        report = output['report_path'].with_name(name).read_text(encoding='utf-8')
        assert ('分析缺失时段' in report) == bool(blocked)
    assert path.read_bytes() == original
    before = len(client.calls)
    assert run_analysis(handoff, client)['reused']
    assert len(client.calls) == before


def test_unrelated_error_is_not_skipped(tmp_path):
    handoff = make_session(tmp_path, short_window_count=1)
    class Client(RecordingClient):
        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            raise AnalysisAPIError('analysis API HTTP 400')
    client = Client()
    with pytest.raises(AnalysisAPIError):
        run_analysis(handoff, client)
    assert len(client.calls) == 1


def test_non_content_error_in_half_still_fails(tmp_path):
    handoff = make_session(tmp_path, short_window_count=1)
    class Client(RecordingClient):
        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise ContentInspectionError()
            raise AnalysisAPIError('analysis API HTTP 401')
    client = Client()
    with pytest.raises(AnalysisAPIError, match='401'):
        run_analysis(handoff, client)
    assert len(client.calls) == 2


def test_empty_half_does_not_call_api(tmp_path):
    handoff = make_session(tmp_path, short_window_count=1)
    class Client(RecordingClient):
        def complete_json(self, **kwargs):
            if not self.calls:
                self.calls.append(kwargs)
                raise ContentInspectionError()
            return super().complete_json(**kwargs)
    client = Client()
    output = run_analysis(handoff, client)
    assert len(client.calls) == 4
    assert output['result']['short_summaries'][0]['analysis_parts'][1]['status'] == 'silent'
