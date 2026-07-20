"""Unit tests for the LinkedIn rate-limit backoff (shared/scraper/job_scraper.py).

These pin the resilience contract of `_linkedin_get`: a 429 no longer folds on
the first hit (the failure that stored 1035 blank descriptions in commit
a5d84f7), a Retry-After header is honoured, and a throttle that survives the
retry budget still raises so the caller's `except` returns []. `requests.get`
and `time.sleep` are patched, so no network is touched and no real time passes.
"""
from unittest import mock

import pytest
import requests

from shared.scraper import job_scraper


def _resp(status, *, text="ok", retry_after=None):
    r = mock.Mock(spec=requests.Response)
    r.status_code = status
    r.text = text
    r.headers = {} if retry_after is None else {"Retry-After": retry_after}
    r.raise_for_status = mock.Mock(
        side_effect=(requests.exceptions.HTTPError(response=r)
                     if status >= 400 else None))
    return r


@pytest.fixture(autouse=True)
def _no_real_sleep():
    with mock.patch.object(job_scraper.time, "sleep") as slept:
        yield slept


def test_happy_path_returns_immediately():
    with mock.patch.object(job_scraper.requests, "get",
                           return_value=_resp(200)) as get:
        resp = job_scraper._linkedin_get("http://x", timeout=5)
    assert resp.status_code == 200
    assert get.call_count == 1


def test_retries_then_succeeds(_no_real_sleep):
    seq = [_resp(429), _resp(429), _resp(200, text="body")]
    with mock.patch.object(job_scraper.requests, "get",
                           side_effect=seq) as get:
        resp = job_scraper._linkedin_get("http://x", timeout=5, max_retries=3)
    assert resp.text == "body"
    assert get.call_count == 3
    assert _no_real_sleep.call_count == 2  # one sleep between each retry


def test_raises_after_budget_exhausted(_no_real_sleep):
    with mock.patch.object(job_scraper.requests, "get",
                           return_value=_resp(429)) as get:
        with pytest.raises(requests.exceptions.HTTPError):
            job_scraper._linkedin_get("http://x", timeout=5, max_retries=2)
    assert get.call_count == 3          # initial + 2 retries
    assert _no_real_sleep.call_count == 2


def test_retry_after_header_is_honoured(_no_real_sleep):
    seq = [_resp(429, retry_after="7"), _resp(200)]
    with mock.patch.object(job_scraper.requests, "get", side_effect=seq):
        job_scraper._linkedin_get("http://x", timeout=5)
    # The single backoff sleep must be >= the 7s the server asked for
    # (jitter only ever adds), and under the RETRY_MAX_DELAY ceiling + jitter.
    slept_for = _no_real_sleep.call_args_list[0].args[0]
    assert 7.0 <= slept_for <= job_scraper.RETRY_MAX_DELAY * 1.25


def test_non_429_error_is_not_retried():
    with mock.patch.object(job_scraper.requests, "get",
                           return_value=_resp(500)) as get:
        with pytest.raises(requests.exceptions.HTTPError):
            job_scraper._linkedin_get("http://x", timeout=5)
    assert get.call_count == 1          # 500 is not a throttle; fail fast


def test_connection_error_is_retried(_no_real_sleep):
    err = requests.exceptions.ConnectionError("reset by peer")
    seq = [err, _resp(200)]
    with mock.patch.object(job_scraper.requests, "get", side_effect=seq) as get:
        resp = job_scraper._linkedin_get("http://x", timeout=5)
    assert resp.status_code == 200
    assert get.call_count == 2


def test_retry_after_parses_only_numeric():
    r = _resp(429, retry_after="30")
    exc = requests.exceptions.HTTPError(response=r)
    assert job_scraper._retry_after_seconds(exc) == 30.0

    r_bad = _resp(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT")
    exc_bad = requests.exceptions.HTTPError(response=r_bad)
    assert job_scraper._retry_after_seconds(exc_bad) is None

    assert job_scraper._retry_after_seconds(ValueError("no response")) is None
