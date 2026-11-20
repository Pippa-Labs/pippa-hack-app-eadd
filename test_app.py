"""Tests for Free Food London. The build runs these on every push.

The network is blocked here (conftest.py), so every source is stubbed
through the one seam, `App._fetch`. The postcodes.io, Nominatim and Devpost
samples are trimmed copies of real responses fetched on 2026-11-20. The
Overpass sample follows Overpass's real JSON shape (`elements`, with `lat`/
`lon` on nodes and `center` on ways); its places are made up.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner, OutputShapeViolation, Result

from app import App, NeedAPlace, NothingFound, PlaceNotFound, SourcesDown

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
OUTCOME = "freefood.near"

# ── Real responses (trimmed) ────────────────────────────────────────────────

POSTCODE_SE1_7PB = {
    "status": 200,
    "result": {"postcode": "SE1 7PB", "latitude": 51.501972, "longitude": -0.119003,
               "region": "London", "admin_district": "Lambeth"},
}
OUTCODE_E1 = {
    "status": 200,
    "result": {"outcode": "E1", "latitude": 51.51738594459098,
               "longitude": -0.05935232783641172, "region": None,
               "admin_district": ["City of London", "Hackney", "Tower Hamlets"]},
}
POSTCODE_MANCHESTER = {
    "status": 200,
    "result": {"postcode": "M1 1AE", "latitude": 53.4808, "longitude": -2.2426,
               "region": "North West"},
}
NOMINATIM_KINGS_CROSS = [{
    "place_id": 280775640, "lat": "51.5323954", "lon": "-0.1230224",
    "name": "London King's Cross", "type": "station",
    "display_name": "London King's Cross, Euston Road, King's Cross, London "
                    "Borough of Camden, Greater London, England, N1C 4AP, United Kingdom",
}]
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

# ── Overpass, in its real shape ─────────────────────────────────────────────

OVERPASS = {
    "version": 0.6,
    "generator": "Overpass API",
    "elements": [
        {"type": "node", "id": 1, "lat": 51.4930, "lon": -0.1080,
         "tags": {"amenity": "place_of_worship", "religion": "sikh",
                  "name": "Example Gurdwara"}},
        # The same gurdwara mapped again as an outline: counted once.
        {"type": "way", "id": 2, "center": {"lat": 51.4931, "lon": -0.1081},
         "tags": {"amenity": "place_of_worship", "religion": "sikh",
                  "name": "Example Gurdwara", "building": "yes"}},
        {"type": "node", "id": 3, "lat": 51.5050, "lon": -0.1150,
         "tags": {"amenity": "food_sharing", "name": "Example Community Fridge",
                  "opening_hours": "Mo-Fr 10:00-18:00"}},
        {"type": "node", "id": 4, "lat": 51.5100, "lon": -0.1250,
         "tags": {"amenity": "give_box"}},
        # Need-based food aid: never returned.
        {"type": "node", "id": 5, "lat": 51.5021, "lon": -0.1191,
         "tags": {"amenity": "food_sharing", "name": "Example Foodbank",
                  "social_facility": "food_bank"}},
        {"type": "node", "id": 6, "lat": 51.5022, "lon": -0.1192,
         "tags": {"amenity": "social_facility", "social_facility": "soup_kitchen",
                  "name": "Example Soup Kitchen"}},
        # A different place of worship: not a gurdwara, not returned.
        {"type": "node", "id": 7, "lat": 51.5023, "lon": -0.1193,
         "tags": {"amenity": "place_of_worship", "religion": "christian", "name": "Example Church"}},
    ],
}


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.com/", code, "error", {}, None)


def stubbed(**answers) -> App:
    """An App whose `_fetch` answers by host: a dict/list, bytes, or an exception."""
    hosts = {
        "postcodes": "api.postcodes.io",
        "nominatim": "nominatim.openstreetmap.org",
        "overpass": "overpass-api.de",
        "devpost": "devpost.com",
    }
    app = App()
    app.urls = []

    def fake(url, timeout=7.0):
        app.urls.append(url)
        for key, host in hosts.items():
            if f"://{host}/" in url:
                answer = answers.get(key, AssertionError(f"{key} should not be called"))
                if isinstance(answer, BaseException):
                    raise answer
                return answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        raise AssertionError(f"unexpected url {url}")

    app._fetch = fake
    return app


def call(app: App, location: str, *, explicit: bool = False) -> Result:
    return LocalRunner(app).call(
        OUTCOME, {"location": location}, granted_secrets={}, now=NOW,
        request_id="test-1", explicit=explicit,
    ).result


def all_up(**overrides) -> App:
    answers = {"postcodes": POSTCODE_SE1_7PB, "nominatim": NOMINATIM_KINGS_CROSS,
               "overpass": OVERPASS, "devpost": DEVPOST}
    answers.update(overrides)
    return stubbed(**answers)


# ── The happy path ──────────────────────────────────────────────────────────


def test_the_manifest_is_valid():
    app = App()
    assert app.manifest.app_id == "hack.app_eadd"
    assert [o.id for o in app.manifest.outcomes] == [OUTCOME]


def test_a_postcode_finds_places_nearest_first_and_london_hackathons():
    app = all_up()
    result = call(app, "SE1 7PB")
    places = result.data["places"]

    assert [p.split(",")[0] for p in places] == [
        "Example Community Fridge", "Give box", "Example Gurdwara"]
    distances = [int(p.split(", ")[2].removesuffix(" m")) for p in places]
    assert distances == sorted(distances)

    fridge, _, gurdwara = places
    assert "community fridge" in fridge and "open Mo-Fr 10:00-18:00" in fridge
    assert "take what you'll use" in fridge
    assert "gurdwara (free vegetarian langar for all)" in gurdwara
    assert "cover your head, shoes off, donations welcome" in gurdwara
    assert all(p.endswith("(source: OpenStreetMap)") for p in places)

    assert result.data["events"] == [
        "WoHack, Nov 21, 2026, Queen Mary University of london, "
        "https://wohack.devpost.com/ (source: Devpost)"]
    assert "OpenStreetMap contributors" in result.data["sources"]
    assert "Devpost" in result.data["sources"]
    assert "Could not reach" not in result.data["sources"]
    assert result.narrative.startswith("The nearest free food to SE1 7PB is Example Community Fridge")
    assert any("api.postcodes.io/postcodes/SE17PB" in u for u in app.urls)


def test_food_banks_and_soup_kitchens_are_never_included():
    places = call(all_up(), "SE1 7PB").data["places"]
    joined = " ".join(places).lower()
    assert "foodbank" not in joined and "soup kitchen" not in joined
    assert "church" not in joined


def test_an_outcode_uses_postcodes_io():
    app = all_up(postcodes=OUTCODE_E1)
    call(app, "e1")
    assert any("api.postcodes.io/outcodes/E1" in u for u in app.urls)


def test_a_station_uses_nominatim_limited_to_london():
    app = all_up()
    result = call(app, "King's Cross station")
    nominatim = [u for u in app.urls if "nominatim" in u][0]
    assert "bounded=1" in nominatim and "viewbox=" in nominatim
    assert "King%27s+Cross+station" in nominatim
    assert result.data["places"]


def test_no_more_than_five_places():
    many = {"elements": [
        {"type": "node", "id": i, "lat": 51.502 + i / 1000, "lon": -0.119,
         "tags": {"amenity": "food_sharing", "name": f"Fridge {i}"}}
        for i in range(1, 9)]}
    places = call(all_up(overpass=many), "SE1 7PB").data["places"]
    assert [p.split(",")[0] for p in places] == [f"Fridge {i}" for i in range(1, 6)]


# ── One source fails: the others are still returned, and named ─────────────


def test_overpass_rate_limited_still_returns_events_and_says_so():
    result = call(all_up(overpass=http_error(429)), "SE1 7PB")
    assert result.data["places"] == []
    assert result.data["events"]
    assert "Could not reach this time: OpenStreetMap (Overpass)." in result.data["sources"]
    assert "could not reach OpenStreetMap (Overpass)" in result.narrative


def test_devpost_timeout_still_returns_places_and_says_so():
    """A timeout is not a URLError: it needs, and has, its own clause."""
    result = call(all_up(devpost=TimeoutError("timed out")), "SE1 7PB")
    assert result.data["places"]
    assert result.data["events"] == []
    assert "Could not reach this time: Devpost." in result.data["sources"]


def test_overpass_giving_up_inside_a_200_counts_as_a_failure():
    gave_up = {"elements": [], "remark": "runtime error: Query timed out"}
    result = call(all_up(overpass=gave_up), "SE1 7PB")
    assert "OpenStreetMap (Overpass)" in result.data["sources"]


def test_an_html_error_page_counts_as_a_failure():
    result = call(all_up(devpost=b"<html>502 Bad Gateway</html>"), "SE1 7PB")
    assert "Could not reach this time: Devpost." in result.data["sources"]


def test_geocoder_down_still_returns_events():
    result = call(all_up(nominatim=http_error(503)), "Camden")
    assert result.data["places"] == []
    assert result.data["events"]
    assert "place lookup" in result.data["sources"]


def test_everything_down_is_a_retryable_error_naming_the_sources():
    app = all_up(overpass=TimeoutError(), devpost=urllib.error.URLError("dns"))
    with pytest.raises(SourcesDown) as failure:
        call(app, "SE1 7PB")
    assert failure.value.retryable is True
    assert "OpenStreetMap (Overpass)" in failure.value.user_message
    assert "Devpost" in failure.value.user_message


# ── Nothing there, or not a place ───────────────────────────────────────────


def test_nothing_found_suggests_a_wider_area():
    empty_devpost = {"hackathons": [], "meta": {"total_count": 0}}
    with pytest.raises(NothingFound) as failure:
        call(all_up(overpass={"elements": []}, devpost=empty_devpost), "SE1 7PB")
    assert "wider area" in failure.value.user_message
    assert "SE1 7PB" in failure.value.user_message


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


def test_a_postcode_outside_london_is_refused():
    with pytest.raises(PlaceNotFound):
        call(all_up(postcodes=POSTCODE_MANCHESTER), "M1 1AE")


# ── No place named ──────────────────────────────────────────────────────────


def test_no_place_returns_london_hackathons_without_guessing_a_place():
    app = stubbed(devpost=DEVPOST)  # any geocode or Overpass call would fail
    result = call(app, "")
    assert result.data["places"] == []
    assert len(result.data["events"]) == 1
    assert "tell me an area" in result.narrative


def test_no_place_and_no_events_asks_for_a_place():
    with pytest.raises(NeedAPlace) as failure:
        call(stubbed(devpost={"hackathons": []}), "")
    assert "London area, postcode or station" in failure.value.user_message


# ── Picked with @, and the contract ─────────────────────────────────────────


def test_picked_by_name_it_answers_and_refuses_as_usual():
    picked = call(all_up(), "SE1 7PB", explicit=True)
    assert picked.data == call(all_up(), "SE1 7PB").data
    with pytest.raises(PlaceNotFound):
        call(all_up(nominatim=[]), "Narnia", explicit=True)


def test_the_runner_enforces_the_manifest(monkeypatch):
    monkeypatch.setattr(App, "handle", lambda self, c: Result(data={"surprise": "hello"}))
    with pytest.raises(OutputShapeViolation):
        call(App(), "SE1 7PB")
