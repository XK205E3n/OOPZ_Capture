from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_analysis_pipeline import ConcurrentOpenCodeClient, make_session
from test_deepseek_client import config, success, set_analyzer_env
from oopz_capture.analysis_pipeline import run_analysis, _window_parallelism, _stage_tokens
from oopz_capture.deepseek_client import DeepSeekClient, DeepSeekConfig, RetryableAnalysisAPIError


@pytest.mark.parametrize('provider', ['deepseek', 'openai-compatible', 'opencode-go'])
def test_every_provider_reaches_configured_concurrency(tmp_path, monkeypatch, provider):
    monkeypatch.setenv('OOPZ_ANALYSIS_MAX_PARALLELISM', '4')
    client = ConcurrentOpenCodeClient()
    client.config.provider = provider
    client.config.base_url = 'https://changed.example.test/v1'
    output = run_analysis(make_session(tmp_path, short_window_count=5), client)
    assert client.peak_active_calls == 4
    assert output['result']['analysis_profile']['window_parallelism'] == 4


@pytest.mark.parametrize('provider', ['deepseek', 'openai-compatible', 'opencode-go'])
@pytest.mark.parametrize('setting', ['0', '9', 'not-an-integer'])
def test_invalid_parallelism_is_never_silently_ignored(monkeypatch, provider, setting):
    monkeypatch.setenv('OOPZ_ANALYSIS_MAX_PARALLELISM', setting)
    with pytest.raises(ValueError):
        _window_parallelism(SimpleNamespace(config=SimpleNamespace(provider=provider)))


@pytest.mark.parametrize('provider', ['deepseek', 'openai-compatible', 'opencode-go'])
def test_changed_connection_and_generation_settings_reach_request(monkeypatch, provider):
    set_analyzer_env(monkeypatch, ANALYZER_PROVIDER=provider,
        ANALYZER_BASE_URL='https://changed.example.test/v1/', ANALYZER_MODEL='changed-model',
        ANALYZER_TIMEOUT_SECONDS='45', ANALYZER_MAX_RETRIES='1', ANALYZER_MIN_INTERVAL_SECONDS='0',
        ANALYZER_MAX_TOKENS='777', ANALYZER_THINKING_MAX_TOKENS='3333',
        ANALYZER_THINKING_MODE='enabled', ANALYZER_THINKING_FORMAT='qwen', ANALYZER_JSON_MODE='false')
    cfg = DeepSeekConfig.from_env()
    calls = []
    def transport(endpoint, headers, payload, timeout):
        calls.append(dict(payload))
        assert endpoint == 'https://changed.example.test/v1/chat/completions'
        assert headers['Authorization'] == 'Bearer ' + cfg.api_key
        assert timeout == 45
        assert payload['model'] == 'changed-model'
        assert payload['enable_thinking'] is True
        assert 'response_format' not in payload and 'thinking' not in payload
        if len(calls) == 1:
            raise RetryableAnalysisAPIError('temporary')
        return success({'summary': 'ok'})
    client = DeepSeekClient(cfg, transport=transport, sleeper=lambda _: None)
    assert _stage_tokens(client, 'disabled', 1024) == 777
    assert _stage_tokens(client, 'enabled', 4096) == 3333
    client.complete_json(system_prompt='Return JSON', user_prompt='JSON', required_keys={'summary': str},
                         max_tokens=_stage_tokens(client, 'enabled', 4096))
    assert len(calls) == 2 and calls[0]['max_tokens'] == 3333


@pytest.mark.parametrize('field,value', [('thinking_mode','enabled'),('thinking_format','qwen'),
    ('json_mode',False),('max_tokens',4444),('thinking_max_tokens',5000),
    ('provider','openai-compatible'),('base_url','https://other.example.test/v1'),('model','other-model')])
def test_output_settings_separate_caches(field, value):
    cfg = config()
    assert DeepSeekClient(cfg).analysis_profile() != DeepSeekClient(replace(cfg, **{field:value})).analysis_profile()


def test_transport_settings_and_key_rotation_preserve_completed_cache():
    cfg = config()
    profile = DeepSeekClient(cfg).analysis_profile()
    changed = replace(cfg, api_key=config().api_key, timeout_seconds=99, max_retries=5, min_interval_seconds=2)
    assert profile == DeepSeekClient(changed).analysis_profile()
    assert cfg.api_key not in str(profile) and changed.api_key not in str(profile)


def test_opencode_session_header_is_stable_and_host_scoped():
    observed = []
    def transport(endpoint, headers, payload, timeout):
        observed.append(headers)
        return success({'summary':'ok'})
    client = DeepSeekClient(config(base_url='https://opencode.ai/zen/go/v1'),transport=transport)
    for _ in range(2):
        client.complete_json(system_prompt='JSON',user_prompt='JSON',required_keys={})
    assert observed[0]['x-opencode-session'] == observed[1]['x-opencode-session']
