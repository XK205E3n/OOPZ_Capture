import json
from uuid import UUID

from oopz_capture.continuous import no_text_marker, _merge_transcripts
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


def test_silent_chunks_merge_with_unique_stable_uuids(tmp_path):
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
    values = [json.loads(line) for line in (session / 'transcript.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len({v['segment_id'] for v in values}) == 2
