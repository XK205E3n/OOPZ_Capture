from uuid import UUID

import pytest

from oopz_capture.continuous import no_text_marker, _merge_transcripts
from oopz_capture.analyzer_job import _load_transcript
from oopz_capture.output import write_json, write_jsonl


def chunk(tmp_path, index):
    path = tmp_path / 'session' / 'chunks' / str(index)
    write_json(path / 'session.json', {
        'session_id': str(index), 'duration_seconds': 300,
        'started_at': '2026-09-12T00:00:00+00:00',
    })
    write_json(path / 'chunk.json', {
        'chunk_id': str(index), 'chunk_index': index,
        'session_offset_ms': index * 300000,
    })
    return path


def test_silent_chunks_merge_and_load_as_unique_stable_uuids(tmp_path):
    results = []
    for index in range(2):
        path = chunk(tmp_path, index)
        marker = no_text_marker(path)
        assert marker == no_text_marker(path)
        UUID(marker['segment_id'])
        write_jsonl(path / 'transcript.jsonl', [marker])
        results.append({'ok': True, 'chunk_dir': path})
    session = tmp_path / 'session'
    write_json(session / 'session.json', {'session_id': 'session'})
    _merge_transcripts(session, results)
    values = _load_transcript(session / 'transcript.jsonl', 'session')
    assert len({v['segment_id'] for v in values}) == 2


def test_legacy_silence_is_read_without_rewriting_source(tmp_path):
    marker = no_text_marker(chunk(tmp_path, 0))
    marker.update(segment_id='no-speech', session_id='session')
    second = dict(marker, start_ms=300000, end_ms=600000)
    path = tmp_path / 'transcript.jsonl'
    write_jsonl(path, [marker, second])
    original = path.read_bytes()
    values = _load_transcript(path, 'session')
    assert values == _load_transcript(path, 'session')
    assert path.read_bytes() == original
    assert len({UUID(v['segment_id']) for v in values}) == 2


@pytest.mark.parametrize('changes', [
    {'segment_id': 'broken'}, {'text': 'actual speech'},
    {'transcript_source': 'asr'}, {'agora_uid': 123},
])
def test_unrecognized_invalid_ids_are_still_rejected(tmp_path, changes):
    marker = no_text_marker(chunk(tmp_path, 0))
    marker.update(segment_id='no-speech', session_id='session')
    marker.update(changes)
    path = tmp_path / 'transcript.jsonl'
    write_jsonl(path, [marker])
    with pytest.raises(ValueError, match='segment_id must be a UUID'):
        _load_transcript(path, 'session')


def test_duplicate_legacy_marker_still_rejected(tmp_path):
    marker = no_text_marker(chunk(tmp_path, 0))
    marker.update(segment_id='no-speech', session_id='session')
    path = tmp_path / 'transcript.jsonl'
    write_jsonl(path, [marker, marker])
    with pytest.raises(ValueError, match='duplicate transcript segment_id'):
        _load_transcript(path, 'session')
