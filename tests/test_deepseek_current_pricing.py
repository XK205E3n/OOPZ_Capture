from datetime import datetime
import pytest
from oopz_capture.analysis_pipeline import _pricing_period, _stage_cost, _is_deepseek_billing_record, DEEPSEEK_PRICING_RATES

@pytest.mark.parametrize('stamp,expected', [
    ('2026-09-21T08:59:59+08:00','off_peak'),
    ('2026-09-21T09:00:00+08:00','peak'),
    ('2026-09-21T12:00:00+08:00','off_peak'),
    ('2026-09-21T14:00:00+08:00','peak'),
    ('2026-09-21T18:00:00+08:00','off_peak'),
    ('2026-09-19T10:00:00+08:00','off_peak'),
    ('2026-09-20T10:00:00+08:00','off_peak'),
    ('2026-09-25T10:00:00+08:00','off_peak'),
    ('2026-10-01T15:00:00+08:00','off_peak'),
    ('2026-09-21T01:00:00+00:00','peak'),
])
def test_calendar(stamp, expected):
    assert _pricing_period(datetime.fromisoformat(stamp)) == expected


def test_million_token_flash_prices():
    usage={'prompt_tokens':2000000,'prompt_cache_hit_tokens':1000000,'prompt_cache_miss_tokens':1000000,'completion_tokens':1000000}
    assert _stage_cost(usage,DEEPSEEK_PRICING_RATES['off_peak'])['estimated_cost_rmb'] == 5.02
    assert _stage_cost(usage,DEEPSEEK_PRICING_RATES['peak'])['estimated_cost_rmb'] == 10.04


def test_pro_never_uses_flash_price():
    assert not _is_deepseek_billing_record({'model_requested':'deepseek-flash','model_returned':'deepseek-v4-pro'})
    assert not _is_deepseek_billing_record({'model_requested':'deepseek-v4-pro'})
    assert _is_deepseek_billing_record({'model_requested':'deepseek-flash','model_returned':'deepseek-flash'})
