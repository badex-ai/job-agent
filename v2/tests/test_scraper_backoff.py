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


# --- fetch_job_description: fetch-failure vs empty-but-successful ---------------
# The contract the guard fix pins: a fetch that could not read the page RAISES
# DescriptionFetchError (so the caller can back off / recover later), while a
# fetch that succeeded but found no description div returns "" (an honest empty
# posting). Conflating the two is what stored throttled fetches as silent blanks.

_DETAIL_WITH_DESC = (
    '<div class="description__text">We build platform infra in Python and '
    'TypeScript. Kubernetes a plus. This body is comfortably over a hundred '
    'characters so the repair gate would accept it.</div>'
    '<a class="apply-button" href="https://apply.example/1">Apply</a>'
)


def test_fetch_description_returns_text_on_success():
    with mock.patch.object(job_scraper, "_linkedin_get",
                           return_value=_resp(200, text=_DETAIL_WITH_DESC)):
        desc, apply_link = job_scraper.fetch_job_description(
            "https://www.linkedin.com/jobs/view/some-role-at-acme-123")
    assert "platform infra" in desc
    assert apply_link == "https://apply.example/1"


def test_fetch_description_empty_posting_returns_blank_not_raise():
    # 200 OK, but no description__text div -> a real empty posting, not a failure.
    with mock.patch.object(job_scraper, "_linkedin_get",
                           return_value=_resp(200, text="<div>no body here</div>")):
        desc, apply_link = job_scraper.fetch_job_description(
            "https://www.linkedin.com/jobs/view/some-role-at-acme-123")
    assert desc == ""
    assert apply_link == ""


def test_fetch_description_raises_when_fetch_fails():
    # A throttle that survived the retry budget: _linkedin_get raises HTTPError.
    # fetch_job_description must surface it as DescriptionFetchError, NOT swallow
    # it into a blank the caller cannot distinguish from an empty posting.
    boom = requests.exceptions.HTTPError("429 Too Many Requests")
    with mock.patch.object(job_scraper, "_linkedin_get", side_effect=boom):
        with pytest.raises(job_scraper.DescriptionFetchError):
            job_scraper.fetch_job_description(
                "https://www.linkedin.com/jobs/view/some-role-at-acme-123")


def _http_error(status):
    # An HTTPError shaped like the one requests' raise_for_status() emits:
    # message + a .response carrying the status code.
    r = mock.Mock(spec=requests.Response)
    r.status_code = status
    return requests.exceptions.HTTPError(f"{status} error", response=r)


@pytest.mark.parametrize("status", [404, 410])
def test_fetch_description_gone_posting_returns_blank_not_raise(status):
    # A 404/410 means the posting is permanently gone, not that the fetch is
    # failing. It must be treated like an empty posting (return "", skip fast),
    # NOT raised as DescriptionFetchError — otherwise a batch of expired rows
    # sits in the 120s retry backoff. This is the behaviour the first live
    # repair run exposed on row 1 (a 404 posting).
    with mock.patch.object(job_scraper, "_linkedin_get",
                           side_effect=_http_error(status)):
        desc, apply_link = job_scraper.fetch_job_description(
            "https://www.linkedin.com/jobs/view/expired-role-at-acme-999")
    assert (desc, apply_link) == ("", "")


def test_fetch_description_transient_http_error_still_raises():
    # A 429/503 carries a response too, but it is transient — it must still raise
    # so the caller backs off, unlike the permanent 404/410 above.
    with mock.patch.object(job_scraper, "_linkedin_get",
                           side_effect=_http_error(429)):
        with pytest.raises(job_scraper.DescriptionFetchError):
            job_scraper.fetch_job_description(
                "https://www.linkedin.com/jobs/view/throttled-role-at-acme-1")


def test_http_status_extracts_or_none():
    assert job_scraper._http_status(_http_error(404)) == 404
    assert job_scraper._http_status(requests.exceptions.ConnectionError("x")) is None
    assert job_scraper._http_status(ValueError("no response")) is None
