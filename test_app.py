"""Tests for Free Food London. The build runs these on every push.

The network is blocked here (conftest.py), so every source is stubbed
through the one seam, `App._fetch`. The postcodes.io, Nominatim and Devpost
samples are trimmed copies of real responses fetched on 2026-11-20. The
Overpass sample follows Overpass's real JSON shape (`elements`, with `lat`/
`lon` on nodes and `center` on ways); its places are made up. The saved
fallback, osm_london.json, is real OpenStreetMap data and is used as it is.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import date, datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner, OutputShapeViolation, Result

from pathlib import Path

import app as app_module
from app import App, NothingFound, PlaceNotFound, SourcesDown, _devpost_dates

# Friday 20 November 2026, midday in London. WoHack is on Saturday the 21st.
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
OUTCOME = "freefood.near"

# ── Real responses (trimmed) ────────────────────────────────────────────────

POSTCODE_SE1_7PB = {
    "status": 200,
    "result": {"postcode": "SE1 7PB", "latitude": 51.501972, "longitude": -0.119003,
               "region": "London", "admin_district": "Lambeth"},
}
NOMINATIM_SOUTHALL = [{"lat": "51.5103", "lon": "-0.3750", "name": "Southall", "type": "town"}]
DEVPOST = {
    "hackathons": [
        # Hack Western is in London, Ontario: its location does not say
        # London, so it must be left out.
        {"id": 31536, "title": "Hack Western 13", "url": "https://hack-western-13.devpost.com/",
         "displayed_location": {"icon": "map-marker-alt", "location": "Ronald D. Schmeichel Building"},
         "open_state": "upcoming", "submission_period_dates": "Nov 20 - 22, 2026"},
        {"id": 31998, "title": "WoHack", "url": "https://wohack.devpost.com/",
         "displayed_location": {"icon": "map-marker-alt", "location": "Queen Mary University of london"},
         "open_state": "upcoming", "submission_period_dates": "Nov 21, 2026"},
    ],
    "meta": {"total_count": 2, "per_page": 9, "fuzzy": False},
}
WOHACK = ("WoHack, hackathon (usually fed), Nov 21, 2026, Queen Mary University of london, "
          "https://wohack.devpost.com/ (source: Devpost)")

# ── Overpass, in its real shape, around SE1 7PB ─────────────────────────────

OVERPASS = {
    "version": 0.6,
    "elements": [
        {"type": "node", "id": 1, "lat": 51.4930, "lon": -0.1080,
         "tags": {"amenity": "place_of_worship", "religion": "sikh", "name": "Example Gurdwara"}},
        # The same gurdwara mapped again as an outline: counted once.
        {"type": "way", "id": 2, "center": {"lat": 51.4931, "lon": -0.1081},
         "tags": {"amenity": "place_of_worship", "religion": "sikh", "name": "Example Gurdwara"}},
        {"type": "node", "id": 3, "lat": 51.5050, "lon": -0.1150,
         "tags": {"amenity": "food_sharing", "name": "Example Community Fridge",
                  "opening_hours": "Mo-Fr 10:00-18:00"}},
        {"type": "node", "id": 4, "lat": 51.5100, "lon": -0.1250, "tags": {"amenity": "give_box"}},
        # Need-based food aid: never returned.
        {"type": "node", "id": 5, "lat": 51.5021, "lon": -0.1191,
         "tags": {"amenity": "food_sharing", "name": "Example Foodbank", "social_facility": "food_bank"}},
    ],
}


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.com/", code, "error", {}, None)


HOSTS = {
    "postcodes": "api.postcodes.io",
    "nominatim": "nominatim.openstreetmap.org",
    "overpass": "overpass-api.de",
    "mirror": "overpass.kumi.systems",
    "devpost": "devpost.com",
}


def stubbed(**answers) -> App:
    """An App whose `_fetch` answers by host: a dict/list, bytes, or an exception."""
    app = App()
    app.urls = []

    def fake(url, timeout=6.0):
        app.urls.append(url)
        for key, host in HOSTS.items():
            if f"://{host}/" in url:
                answer = answers.get(key, SourcesDownInTest(key))
                if isinstance(answer, BaseException):
                    raise answer
                return answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        raise AssertionError(f"unexpected url {url}")

    app._fetch = fake
    return app


def SourcesDownInTest(key):  # noqa: N802 - reads as an exception
    return urllib.error.URLError(f"{key} not stubbed in this test")


def all_up(**overrides) -> App:
    answers = {"postcodes": POSTCODE_SE1_7PB, "nominatim": NOMINATIM_SOUTHALL,
               "overpass": OVERPASS, "mirror": http_error(503), "devpost": DEVPOST}
    answers.update(overrides)
    return stubbed(**answers)


def call(app: App, location: str, *, when: str = "", cuisine: str = "",
         explicit: bool = False) -> Result:
    return LocalRunner(app).call(
        OUTCOME, {"location": location, "when": when, "cuisine": cuisine},
        granted_secrets={}, now=NOW, request_id="test-1", explicit=explicit,
    ).result


# ── The happy path ──────────────────────────────────────────────────────────


def test_the_manifest_is_valid():
    app = App()
    assert app.manifest.app_id == "hack.app_eadd"
    assert [o.id for o in app.manifest.outcomes] == [OUTCOME]


def test_places_nearest_first_and_london_hackathons():
    result = call(all_up(), "SE1 7PB")
    places = result.data["places"]
    assert [p.split(",")[0] for p in places] == [
        "Example Community Fridge", "Give box", "Example Gurdwara"]
    fridge, _, gurdwara = places
    assert "open Mo-Fr 10:00-18:00" in fridge and "take what you'll use" in fridge
    assert "cover your head, shoes off, donations welcome" in gurdwara
    assert "foodbank" not in " ".join(places).lower()
    assert result.data["events"] == [WOHACK]
    assert "OpenStreetMap contributors" in result.data["sources"]
    assert "note" not in result.data
    assert result.narrative.startswith("The nearest free food to SE1 7PB is Example Community Fridge")


# ── Reliability: mirror, saved data, one source down ────────────────────────


def test_overpass_down_the_mirror_answers():
    result = call(all_up(overpass=http_error(504), mirror=OVERPASS), "SE1 7PB")
    assert len(result.data["places"]) == 3
    assert "note" not in result.data


def test_both_overpass_servers_down_uses_the_saved_real_data():
    result = call(all_up(overpass=http_error(429), mirror=TimeoutError()), "Southall")
    names = [p.split(",")[0] for p in result.data["places"]]
    assert names[0] == "Guru Granth Gurdwara"  # real, from osm_london.json
    assert len(names) == 5
    assert "saved OpenStreetMap data from 2026-11-20" in result.data["note"]
    assert "saved snapshot" in result.data["sources"]


def test_devpost_timeout_still_returns_places_and_says_so():
    """A timeout is not a URLError: it needs, and has, its own clause."""
    result = call(all_up(devpost=TimeoutError("timed out")), "SE1 7PB")
    assert result.data["places"]
    assert result.data["events"] == []
    assert "Could not reach this time: Devpost." in result.data["sources"]


def test_an_html_error_page_counts_as_a_failure():
    result = call(all_up(devpost=b"<html>502 Bad Gateway</html>"), "SE1 7PB")
    assert "Could not reach this time: Devpost." in result.data["sources"]


def no_saved_data(monkeypatch):
    monkeypatch.setattr(app_module, "SNAPSHOT_PATH", Path("/nonexistent/osm_london.json"))


def test_everything_down_is_a_retryable_error_naming_the_sources(monkeypatch):
    no_saved_data(monkeypatch)
    app = all_up(overpass=TimeoutError(), devpost=urllib.error.URLError("dns"))
    with pytest.raises(SourcesDown) as failure:
        call(app, "SE1 7PB")
    assert failure.value.retryable is True
    assert "OpenStreetMap (Overpass)" in failure.value.user_message
    assert "Devpost" in failure.value.user_message


# ── Every path answers ──────────────────────────────────────────────────────


def assert_answers(result: Result):
    assert result.narrative and result.narrative.strip()
    assert result.data["places"] or result.data["events"]


def test_i_want_piazza_where_food():
    """"@Free Food London i want piazza where food" gave an empty reply.

    Pippa may extract it any of these ways; every one must answer.
    "piazza" as a place is a real spot in Bromley-by-Bow, with nothing
    mapped within 2 km of it.
    """
    nominatim_piazza = [{"lat": "51.5254552", "lon": "-0.0104496", "name": "Piazza"}]
    for inputs in ({"location": "", "cuisine": "piazza"},
                   {"location": "", "cuisine": "pizza"},
                   {"location": "", "cuisine": ""},
                   {"location": "piazza", "cuisine": "piazza"}):
        app = all_up(nominatim=nominatim_piazza, overpass={"elements": []})
        result = call(app, inputs["location"], cuisine=inputs["cuisine"])
        assert_answers(result)
        assert result.data["places"], inputs
        if inputs["cuisine"]:
            assert "Nothing matched piazza" in result.data["note"] or \
                "Nothing matched pizza" in result.data["note"]


def test_no_area_searches_all_of_london_and_asks_for_an_area():
    app = stubbed(devpost=DEVPOST)  # a geocode or Overpass call would fail
    result = call(app, "")
    assert_answers(result)
    assert len(result.data["places"]) == 5
    assert all("m from central London" in p for p in result.data["places"])
    assert result.data["events"] == [WOHACK]
    assert "name an area for nearby results" in result.data["note"].lower()
    assert "saved snapshot" in result.data["sources"]


def test_free_food_near_camden():
    """0.1.0 gave an empty reply: nothing is mapped within 2 km of Camden."""
    nominatim_camden = [{"lat": "51.5427966", "lon": "-0.1624803", "name": "Camden"}]
    for overpass in ({"elements": []}, http_error(504)):  # live empty, or down
        result = call(all_up(nominatim=nominatim_camden, overpass=overpass), "camden")
        assert_answers(result)
        assert result.data["places"]
        assert "Nothing is mapped within 2 km of camden" in result.data["note"]
        assert result.narrative.startswith("The nearest free food to camden is ")


# ── Nothing there, or not a place ───────────────────────────────────────────


def test_nothing_found_suggests_a_wider_area(monkeypatch):
    no_saved_data(monkeypatch)
    with pytest.raises(NothingFound) as failure:
        call(all_up(overpass={"elements": []}, devpost={"hackathons": []}), "SE1 7PB")
    assert "wider area" in failure.value.user_message


def test_a_place_nominatim_has_never_heard_of_is_named():
    """Nominatim answers 200 with [] for a place that does not exist."""
    with pytest.raises(PlaceNotFound) as failure:
        call(all_up(nominatim=[]), "Narnia")
    assert failure.value.user_message.startswith("I could not find Narnia in London.")


def test_an_unknown_postcode_is_named():
    """postcodes.io answers 404 for a postcode that does not exist."""
    with pytest.raises(PlaceNotFound) as failure:
        call(all_up(postcodes=http_error(404)), "ZZ9 9ZZ")
    assert "ZZ9 9ZZ" in failure.value.user_message


# ── when ────────────────────────────────────────────────────────────────────


def test_when_filters_events_to_the_window():
    assert call(all_up(), "SE1 7PB", when="saturday").data["events"] == [WOHACK]
    assert call(all_up(), "SE1 7PB", when="this weekend").data["events"] == [WOHACK]
    assert call(all_up(), "SE1 7PB", when="next week").data["events"] == []


def test_a_when_it_cannot_read_falls_back_to_14_days_and_says_so():
    result = call(all_up(), "SE1 7PB", when="whenever-ish")
    assert result.data["events"] == [WOHACK]
    assert "next 14 days" in result.data["note"]


def test_devpost_date_ranges_are_read():
    assert _devpost_dates("Nov 21, 2026") == (date(2026, 11, 21), date(2026, 11, 21))
    assert _devpost_dates("Nov 20 - 22, 2026") == (date(2026, 11, 20), date(2026, 11, 22))
    assert _devpost_dates("Nov 28 - Dec 01, 2026") == (date(2026, 11, 28), date(2026, 12, 1))
    assert _devpost_dates("Dec 30, 2026 - Jan 02, 2027") == (date(2026, 12, 30), date(2027, 1, 2))
    assert _devpost_dates("soon") == (None, None)


# ── cuisine ─────────────────────────────────────────────────────────────────


def test_langar_matches_indian_and_the_rest_is_said():
    result = call(all_up(), "SE1 7PB", cuisine="indian")
    assert [p.split(",")[0] for p in result.data["places"]] == ["Example Gurdwara"]
    assert result.data["events"] == [WOHACK]  # no event matched, so all are kept
    assert "Punjabi vegetarian" in result.data["note"]
    assert "No events matched indian" in result.data["note"]


def test_langar_is_not_vegan():
    result = call(all_up(), "SE1 7PB", cuisine="vegan")
    assert len(result.data["places"]) == 3
    assert "Nothing matched vegan" in result.data["note"]


def test_a_cuisine_in_an_event_title_matches():
    pizza = {"hackathons": [dict(DEVPOST["hackathons"][1], title="Pizza Hack")]}
    result = call(all_up(devpost=pizza), "SE1 7PB", cuisine="pizza")
    assert result.data["events"][0].startswith("Pizza Hack, hackathon (usually fed)")
    assert len(result.data["places"]) == 3  # no place matched, so all are kept


# ── Picked with @, and the contract ─────────────────────────────────────────


def test_picked_by_name_it_answers_and_refuses_as_usual():
    assert call(all_up(), "SE1 7PB", explicit=True).data == call(all_up(), "SE1 7PB").data
    with pytest.raises(PlaceNotFound):
        call(all_up(nominatim=[]), "Narnia", explicit=True)


def test_the_runner_enforces_the_manifest(monkeypatch):
    monkeypatch.setattr(App, "handle", lambda self, c: Result(data={"surprise": "hello"}))
    with pytest.raises(OutputShapeViolation):
        call(App(), "SE1 7PB")
