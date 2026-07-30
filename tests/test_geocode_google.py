"""
test_geocode_google.py - unit tests for 07b_geocode_entities_google.py's
core logic (response handling, retries, edge cases). 

Usage:
    pytest tests/test_geocode_google.py -v
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# The script's filename starts with a digit, which isn't a valid Python
# module name for a plain `import` - load it explicitly by file path instead.
MODULE_PATH = Path(__file__).parent.parent / "scripts" / "pipeline" / "07b_geocode_entities_google.py"
spec = importlib.util.spec_from_file_location("geocode_google", MODULE_PATH)
geocode_google = importlib.util.module_from_spec(spec)
sys.modules["geocode_google"] = geocode_google
spec.loader.exec_module(geocode_google)


class FakeResponse:
    """Minimal stand-in for requests.Response - just needs .json()."""
    def __init__(self, json_data):
        self._json_data = json_data

    def json(self):
        return self._json_data


def make_ok_response(lat=37.1, lng=-122.1, location_type="ROOFTOP",
                      formatted="123 Test St, Testville, CA 90001, USA"):
    return FakeResponse({
        "status": "OK",
        "results": [{
            "geometry": {"location": {"lat": lat, "lng": lng},
                         "location_type": location_type},
            "formatted_address": formatted,
        }],
    })


class TestGeocodeGoogleSingle:

    def test_successful_match_returns_correct_values(self):
        with patch.object(geocode_google.requests, "get",
                           return_value=make_ok_response(37.5, -122.5, "ROOFTOP")):
            lat, lon, source, formatted = geocode_google.geocode_google_single(
                "123 Main St", "Testville", "CA", "90001", "fake_key"
            )
        assert lat == 37.5
        assert lon == -122.5
        assert source == "google_rooftop"
        assert formatted is not None

    def test_zero_results_returns_none_with_reason(self):
        with patch.object(geocode_google.requests, "get",
                           return_value=FakeResponse({"status": "ZERO_RESULTS"})):
            lat, lon, source, formatted = geocode_google.geocode_google_single(
                "1 Nowhere Rd", "Nowhere", "CA", "00000", "fake_key"
            )
        assert lat is None
        assert lon is None
        assert source == "failed:zero_results"

    def test_over_query_limit_retries_then_gives_up(self):
        call_count = {"n": 0}

        def fake_get(*args, **kwargs):
            call_count["n"] += 1
            return FakeResponse({"status": "OVER_QUERY_LIMIT"})

        with patch.object(geocode_google.requests, "get", side_effect=fake_get), \
             patch.object(geocode_google.time, "sleep"):  # skip real delays in test
            lat, lon, source, formatted = geocode_google.geocode_google_single(
                "1 Busy Ave", "X", "CA", "00000", "fake_key", retries=2
            )
        assert lat is None
        assert source == "failed:over_query_limit"
        assert call_count["n"] == 3  # original attempt + 2 retries

    def test_over_query_limit_succeeds_on_retry(self):
        """If the SECOND attempt succeeds, we should get a real result,
        not a failure - proves retries actually help when the rate limit
        clears up, not just that we give up gracefully."""
        call_count = {"n": 0}

        def fake_get(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FakeResponse({"status": "OVER_QUERY_LIMIT"})
            return make_ok_response()

        with patch.object(geocode_google.requests, "get", side_effect=fake_get), \
             patch.object(geocode_google.time, "sleep"):
            lat, lon, source, formatted = geocode_google.geocode_google_single(
                "1 Busy Ave", "X", "CA", "00000", "fake_key", retries=2
            )
        assert lat is not None
        assert call_count["n"] == 2

    def test_request_denied_does_not_retry(self):
        """A bad API key/config should fail fast, not waste retries on a
        problem that retrying can't fix."""
        call_count = {"n": 0}

        def fake_get(*args, **kwargs):
            call_count["n"] += 1
            return FakeResponse({"status": "REQUEST_DENIED"})

        with patch.object(geocode_google.requests, "get", side_effect=fake_get):
            lat, lon, source, formatted = geocode_google.geocode_google_single(
                "1 Test Blvd", "X", "CA", "00000", "fake_key", retries=2
            )
        assert lat is None
        assert source == "failed:request_denied"
        assert call_count["n"] == 1  # no retries attempted

    def test_empty_address_makes_no_api_call(self):
        """Should never spend a billed API call on a row with no address
        data at all."""
        call_count = {"n": 0}

        def fake_get(*args, **kwargs):
            call_count["n"] += 1
            return make_ok_response()

        with patch.object(geocode_google.requests, "get", side_effect=fake_get):
            lat, lon, source, formatted = geocode_google.geocode_google_single(
                "", "", "", "", "fake_key"
            )
        assert lat is None
        assert source == "failed:no_address_data"
        assert call_count["n"] == 0

    def test_network_exception_is_caught_not_raised(self):
        """A connection error shouldn't crash the whole batch run - it
        should be caught and recorded as a failure for that one row."""
        def fake_get(*args, **kwargs):
            raise ConnectionError("simulated network failure")

        with patch.object(geocode_google.requests, "get", side_effect=fake_get), \
             patch.object(geocode_google.time, "sleep"):
            lat, lon, source, formatted = geocode_google.geocode_google_single(
                "1 Test Blvd", "X", "CA", "00000", "fake_key", retries=1
            )
        assert lat is None
        assert "exception" in source
        assert "ConnectionError" in source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])