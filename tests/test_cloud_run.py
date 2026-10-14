from datetime import date, datetime
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

import cloud_run as m

CAL = xcals.get_calendar('XHKG')
ASOF = date(2026, 10, 13)


def example(code, kind='fall'):
    sessions = CAL.sessions_in_range(pd.Timestamp('2025-01-01'), pd.Timestamp(ASOF))
    result = []
    for i, day in enumerate(sessions):
        if kind == 'fall':
            # 2025: 20 -> 16; 2026: 16 -> 8; 3 consecutive bearish candles at the end
            frac = i / max(1, len(sessions)-1)
            close = 20 - 12 * frac
            op = close + (0.12 if i >= len(sessions) - 5 else 0.03)
        else:
            close = 15.0
            op = 15.01
        result.append({'date': day.date(), 'open': op, 'high': op + 0.15,
                       'low': close - 0.15, 'close': close, 'volume': 2000, 'amount': 20000})
    return pd.DataFrame(result)


def test_codes_unique():
    from pathlib import Path
    stocks = m.load_stocks(Path(__file__).resolve().parents[1] / 'config' / 'stocks.csv')
    assert len(stocks) == 143
    assert stocks[0]['code'] == '01810'
    assert stocks[134]['code'] == '03896'
    assert stocks[-1]['code'] == '02238'


def test_cutoff_trading_dates():
    assert m.expected_session(datetime(2026, 10, 14, 6, 17, tzinfo=ZoneInfo('Asia/Hong_Kong')), CAL) == ASOF
    assert m.expected_session(datetime(2026, 10, 13, 19, 17, tzinfo=ZoneInfo('Asia/Hong_Kong')), CAL) == ASOF
    assert m.expected_session(datetime(2026, 10, 12, 6, 17, tzinfo=ZoneInfo('Asia/Hong_Kong')), CAL) == date(2026, 10, 9)


def test_normalize_filters_future():
    raw = example('01810')
    future = raw.iloc[-1:].copy()
    future.loc[:, 'date'] = date(2026,10,14)
    raw = pd.concat([raw, future], ignore_index=True)
    norm = m.normalize(raw, ASOF)
    assert norm['date'].max() == ASOF.isoformat()
    assert len(norm) == len(raw)-1


def test_screen_both_year_lows():
    hist = m.normalize(example('01810'), ASOF)
    r = m.calc_stock({'rank':1,'code':'01810','name':'小米集团'}, hist, ASOF, CAL)
    assert r['data_status'] == 'fresh'
    assert r['prior_year_full'] and r['current_year_full_from_start']
    assert r['return_120_sessions_pct'] < -20
    assert r['previous_year_low'] and r['current_year_low']
    assert r['below_previous_year_low']
    assert r['bearish_streak'] >= 3


def test_missing_target_never_candidate():
    raw = example('03896').iloc[:-1]
    hist = m.normalize(raw, ASOF)
    r = m.calc_stock({'rank':135,'code':'03896','name':'金山云'}, hist, ASOF, CAL)
    assert r['data_status'] == 'stale'
    assert not r['previous_year_low'] and not r['current_year_low']


def test_end_to_end_offline_with_failure(tmp_path):
    stocks = [dict(rank=1,code='01810',name='小米集团'),
              dict(rank=2,code='03896',name='金山云')]
    def fake_fetch(symbol, adjust):
        assert adjust == ''
        if symbol == '03896':
            raise OSError('network unavailable for test')
        return example(symbol)
    report = m.run(tmp_path, stocks, ASOF, CAL, attempts=1, delay=0, fetch_func=fake_fetch, sleep=False)
    assert report['meta']['fresh_count'] == 1
    assert report['meta']['missing_count'] == 1
    assert report['meta']['status'] == 'partial'
    assert len(report['ranked_candidates']) == 1
    assert (tmp_path/'data'/'2025'/'01810_2025.csv').exists()
    assert (tmp_path/'data'/'2026'/'01810_2026.csv').exists()
    assert (tmp_path/'data'/'latest.md').exists()
    assert (tmp_path/'data'/'latest.json').exists()
    assert (tmp_path/'data'/'全部低位候选.csv').exists()
    first = (tmp_path/'data'/'latest.json').read_text(encoding='utf-8')
    assert '2026-10-13' in first
    # Cached fresh code should not call the data source twice.
    def error_fetch(symbol,adjust):
        assert symbol != '01810'
        raise OSError('only missing ticker retried')
    report2 = m.run(tmp_path, stocks, ASOF, CAL, attempts=1, delay=0, fetch_func=error_fetch, sleep=False)
    assert report2['meta']['fresh_count'] == 1
    assert report2['meta']['missing_count'] == 1


def test_three_buckets_and_unlimited_ranking():
    # All qualifying stocks remain, even when double-low alone exceeds 20.
    def candidate(index, prev, current, bearish=0):
        return {
            'code': str(index).zfill(5),
            'name': '测试股票' + str(index),
            'previous_year_low': prev,
            'current_year_low': current,
            'previous_year_range': {'position_pct': float(index % 10)},
            'current_year_range': {'position_pct': float(index % 10)},
            'bearish_streak': bearish,
        }
    records = (
        [candidate(i, True, True, 4 if i == 21 else 0) for i in range(1, 22)]
        + [candidate(i, False, True) for i in range(22, 26)]
        + [candidate(i, True, False) for i in range(26, 29)]
        + [candidate(29, False, False)]
    )
    both, current_only, previous_only, ranked = m.prioritize(records)
    assert [len(both), len(current_only), len(previous_only), len(ranked)] == [21, 4, 3, 28]
    assert all(r['bucket'] == '2025与2026双重低位' for r in both)
    assert all(r['bucket'] == '仅2026年低位' for r in current_only)
    assert all(r['bucket'] == '仅2025年低位' for r in previous_only)
    assert [r['rank'] for r in ranked] == list(range(1, 29))
    assert len({r['code'] for r in ranked}) == 28
    assert ranked[0]['code'] == '00021'  # first sort on >=3 bearish sessions


def test_report_contains_all_three_buckets(tmp_path):
    stocks = [dict(rank=1, code='01810', name='小米集团')]
    def fake_fetch(symbol, adjust):
        assert adjust == ''
        return example(symbol)
    report = m.run(tmp_path, stocks, ASOF, CAL, attempts=1, delay=0, fetch_func=fake_fetch, sleep=False)
    assert len(report['double_low_candidates']) == 1
    assert len(report['current_year_only_candidates']) == 0
    assert len(report['previous_year_only_candidates']) == 0
    assert len(report['ranked_candidates']) == 1
    markdown = (tmp_path / 'data' / 'latest.md').read_text(encoding='utf-8')
    assert '双重低位' in markdown
    assert '仅2026年低位' in markdown
    assert '仅2025年低位' in markdown
    assert '| 1 | 01810 小米集团 |' in markdown
    assert '前20' not in markdown
