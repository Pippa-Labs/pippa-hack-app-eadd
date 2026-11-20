"""Free Food London: where a student can get free food near a London place.

One outcome, three live sources, no API keys:

1. Geocode the place: postcodes.io for a postcode or outcode, otherwise
   Nominatim (OpenStreetMap's geocoder), held to a London bounding box.
2. Overpass (OpenStreetMap): community fridges and food-sharing points
   (amenity=food_sharing, amenity=give_box) and gurdwaras, whose langar is a
   free vegetarian meal open to everyone, within about 2 km.
3. Devpost: upcoming in-person hackathons whose location mentions London.

Each source runs in its own thread with a ~7 s timeout. If one fails, the
others are still returned, and `sources` and the narrative say which failed.

Food banks and soup kitchens are never included: they are for people in
need, and this app is for students looking for food that is free to anyone.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlencode

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    Result,
    ServiceUnavailableError,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

#: Nominatim and Overpass ask for a descriptive User-Agent; urllib sends none.
USER_AGENT = "pippa-agent-app-free-food-london/0.1 (https://github.com/Pippa-Labs)"

#: Per-request timeout for each source, in seconds.
TIMEOUT = 7.0
#: The whole call must finish inside Pippa's 20 s; leave some margin.
BUDGET = 17.0
RADIUS_M = 2000
MAX_PLACES = 5
MAX_EVENTS = 3

#: Greater London, with a small margin: south, north, west, east.
LONDON_BOX = (51.28, 51.70, -0.52, 0.34)

POSTCODE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}$")
OUTCODE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?$")

#: Anything mentioning these is need-based food aid, never returned.
NEED_BASED = ("food bank", "foodbank", "soup kitchen")

FRIDGE_TIP = "tip: take what you'll use"
LANGAR_TIP = "tip: cover your head, shoes off, donations welcome"

OSM_CREDIT = (
    "Places © OpenStreetMap contributors (ODbL, openstreetmap.org/copyright), "
    "found with the Overpass API; locations from postcodes.io and Nominatim."
)
DEVPOST_CREDIT = "Events from Devpost (devpost.com/hackathons)."

GEOCODE = "the place lookup (postcodes.io / Nominatim)"
OVERPASS = "OpenStreetMap (Overpass)"
DEVPOST = "Devpost"


class PlaceNotFound(AgentAppError):
    user_message = (
        "I could not find that place in London. Try a London postcode, area "
        "or station, like \"E1\", \"Camden\" or \"King's Cross\"."
    )
    retryable = False


class NeedAPlace(AgentAppError):
    user_message = (
        "Tell me a London area, postcode or station, like \"free food near "
        "Camden\", and I will look for free food there."
    )
    retryable = False


class NothingFound(AgentAppError):
    user_message = (
        "I found no free food there. Try a wider area, like the borough or a "
        "bigger town centre nearby."
    )
    retryable = False


class SourcesDown(ServiceUnavailableError):
    user_message = "I could not reach the free-food sources just now. Please try again shortly."
    retryable = True


class SourceFailed(Exception):
    """One source could not answer. Internal: never shown to the user."""


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    # ── The one seam that touches the network ───────────────────────────────

    def _fetch(self, url: str, timeout: float = TIMEOUT) -> bytes:
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def _get_json(self, url: str):
        """Fetch and parse JSON. A 404 is `None`; any other failure raises."""
        try:
            raw = self._fetch(url, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:  # first: it IS a URLError
            if exc.code == 404:
                return None
            raise SourceFailed(f"HTTP {exc.code}") from None
        except TimeoutError:  # NOT a URLError
            raise SourceFailed("timed out") from None
        except urllib.error.URLError as exc:
            raise SourceFailed(f"unreachable: {exc.reason}") from None
        except (OSError, http.client.HTTPException) as exc:
            raise SourceFailed(f"connection failed: {type(exc).__name__}") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise SourceFailed("the answer was not JSON") from None

    # ── handle ──────────────────────────────────────────────────────────────

    def handle(self, call: Call) -> Result:
        if call.outcome_id != "free_food.near":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        place = " ".join((call.inputs.get("location") or "").split())
        deadline = time.monotonic() + BUDGET
        failed: list[str] = []
        places: list[tuple[int, str, str]] = []
        label = place

        pool = ThreadPoolExecutor(max_workers=3)
        try:
            events_job = pool.submit(self._events)

            if place:
                spot = None
                try:
                    spot = pool.submit(self._geocode, place).result(timeout=TIMEOUT + 1)
                except (SourceFailed, TimeoutError, ValueError, KeyError, TypeError):
                    failed.append(GEOCODE)
                else:
                    if spot is None:
                        error = PlaceNotFound(f"no London coordinates for {place!r}")
                        error.user_message = (
                            f"I could not find {place} in London. Try a London "
                            "postcode, area or station, like \"E1\", \"Camden\" "
                            "or \"King's Cross\"."
                        )
                        raise error
                    lat, lon, label = spot

                if spot is not None:
                    wait = max(0.5, min(TIMEOUT + 1, deadline - time.monotonic()))
                    try:
                        places = pool.submit(self._places, lat, lon).result(timeout=wait)
                    except (SourceFailed, TimeoutError, ValueError, KeyError, TypeError):
                        failed.append(OVERPASS)

            events: list[str] = []
            wait = max(0.5, deadline - time.monotonic())
            try:
                events = events_job.result(timeout=wait)
            except (SourceFailed, TimeoutError, ValueError, KeyError, TypeError):
                failed.append(DEVPOST)
        finally:
            # Do not wait for a source that is still hanging: its answer is
            # no longer wanted, and the user is.
            pool.shutdown(wait=False, cancel_futures=True)

        if not places and not events:
            if not place:
                raise NeedAPlace("no location given and no London hackathons found")
            if failed:
                error = SourcesDown(f"sources failed: {failed}")
                error.user_message = (
                    f"I could not reach {_join(failed)} just now, and found no "
                    f"free food near {label} from the rest. Please try again shortly."
                )
                raise error
            error = NothingFound(f"nothing within {RADIUS_M} m of {label!r}")
            error.user_message = (
                f"I found no community fridges or gurdwaras within 2 km of "
                f"{label}, and no upcoming in-person London hackathons on "
                "Devpost. Try a wider area, like the borough or a bigger town "
                "centre nearby."
            )
            raise error

        sources = OSM_CREDIT + " " + DEVPOST_CREDIT
        if failed:
            sources += f" Could not reach this time: {_join(failed)}."

        return Result(
            data={
                "places": [text for _, _, text in places],
                "events": events,
                "sources": sources,
            },
            narrative=_narrative(place, label, places, events, failed),
            warnings=tuple(f"Could not reach {name}." for name in failed),
        )

    # ── Source 1: geocode ───────────────────────────────────────────────────

    def _geocode(self, place: str):
        """(lat, lon, label) for a London place, or None if there is none."""
        upper = place.upper().strip()
        compact = upper.replace(" ", "")

        if POSTCODE.match(upper) or OUTCODE.match(compact):
            kind = "postcodes" if POSTCODE.match(upper) else "outcodes"
            data = self._get_json(
                f"https://api.postcodes.io/{kind}/" + quote(compact, safe="")
            )
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                return None
            lat, lon = result.get("latitude"), result.get("longitude")
            label = result.get("postcode") or result.get("outcode") or upper
        else:
            west, south, east, north = LONDON_BOX[2], LONDON_BOX[0], LONDON_BOX[3], LONDON_BOX[1]
            data = self._get_json(
                "https://nominatim.openstreetmap.org/search?"
                + urlencode({
                    "q": place,
                    "format": "jsonv2",
                    "limit": 1,
                    "countrycodes": "gb",
                    "viewbox": f"{west},{north},{east},{south}",
                    "bounded": 1,
                })
            )
            if not isinstance(data, list) or not data or not isinstance(data[0], dict):
                return None
            lat, lon = data[0].get("lat"), data[0].get("lon")
            label = place

        if lat is None or lon is None:
            return None
        lat, lon = float(lat), float(lon)
        south, north, west, east = LONDON_BOX
        if not (south <= lat <= north and west <= lon <= east):
            return None  # a real place, but not in London
        return lat, lon, label

    # ── Source 2: OpenStreetMap, through Overpass ───────────────────────────

    def _places(self, lat: float, lon: float) -> list[tuple[int, str, str]]:
        around = f"(around:{RADIUS_M},{lat:.5f},{lon:.5f})"
        query = (
            "[out:json][timeout:6];("
            f'nwr{around}["amenity"="food_sharing"];'
            f'nwr{around}["amenity"="give_box"];'
            f'nwr{around}["amenity"="place_of_worship"]["religion"="sikh"];'
            ");out center tags;"
        )
        data = self._get_json(
            "https://overpass-api.de/api/interpreter?" + urlencode({"data": query})
        )
        if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
            raise SourceFailed("Overpass answered without a list of elements")
        if not data["elements"] and data.get("remark"):
            # Overpass reports its own timeouts as a 200 with a remark.
            raise SourceFailed("Overpass gave up on the query")

        best: dict[tuple[str, str], tuple[int, str, str]] = {}
        for element in data["elements"]:
            if not isinstance(element, dict):
                continue
            tags = element.get("tags") or {}
            kind = _kind(tags)
            if kind is None or _need_based(tags):
                continue
            centre = element.get("center") or {}
            plat = element.get("lat", centre.get("lat"))
            plon = element.get("lon", centre.get("lon"))
            if plat is None or plon is None:
                continue
            metres = int(round(_distance_m(lat, lon, float(plat), float(plon)) / 10) * 10)
            label, tip, unnamed = kind
            name = (tags.get("name") or tags.get("name:en") or unnamed).strip()
            parts = [name, label, f"{metres} m"]
            if tags.get("opening_hours"):
                parts.append(f"open {tags['opening_hours'].strip()}")
            parts.append(tip)
            text = ", ".join(parts) + " (source: OpenStreetMap)"
            # A building is often mapped twice (as a point and an outline).
            key = (name.lower(), label)
            if key not in best or metres < best[key][0]:
                best[key] = (metres, name, text)

        return sorted(best.values())[:MAX_PLACES]

    # ── Source 3: Devpost ───────────────────────────────────────────────────

    def _events(self) -> list[str]:
        data = self._get_json(
            "https://devpost.com/api/hackathons?"
            + urlencode([
                ("challenge_type[]", "in-person"),
                ("status[]", "upcoming"),
                ("search", "London"),
            ])
        )
        if not isinstance(data, dict) or not isinstance(data.get("hackathons"), list):
            raise SourceFailed("Devpost answered without a list of hackathons")

        events: list[str] = []
        for hackathon in data["hackathons"]:
            if not isinstance(hackathon, dict):
                continue
            where = ((hackathon.get("displayed_location") or {}).get("location") or "").strip()
            lowered = where.lower()
            # "London" also matches London, Ontario.
            if "london" not in lowered or "ontario" in lowered or "canada" in lowered:
                continue
            title = (hackathon.get("title") or "").strip()
            link = (hackathon.get("url") or "").strip()
            if not title or not link.startswith("https://"):
                continue
            when = (hackathon.get("submission_period_dates") or "date not given").strip()
            events.append(f"{title}, {when}, {where}, {link} (source: Devpost)")
            if len(events) == MAX_EVENTS:
                break
        return events


# ── Helpers ─────────────────────────────────────────────────────────────────


def _kind(tags: dict):
    """(type label, tip, name to use if it has none) or None."""
    amenity = tags.get("amenity")
    if amenity == "food_sharing":
        return ("community fridge / food-sharing point", FRIDGE_TIP, "Community fridge")
    if amenity == "give_box":
        return ("give box", FRIDGE_TIP, "Give box")
    if amenity == "place_of_worship" and tags.get("religion") == "sikh":
        return ("gurdwara (free vegetarian langar for all)", LANGAR_TIP, "Gurdwara")
    return None


def _need_based(tags: dict) -> bool:
    if tags.get("social_facility") in ("food_bank", "soup_kitchen"):
        return True
    if tags.get("amenity") == "social_facility":
        return True
    text = " ".join(str(tags.get(k, "")) for k in ("name", "description", "operator")).lower()
    return any(word in text for word in NEED_BASED)


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


def _narrative(place, label, places, events, failed) -> str:
    n = len(events)
    hackathons = (
        f"{n} upcoming London hackathon{'s' if n != 1 else ''} on Devpost"
        f"{', which usually feeds participants' if n == 1 else ', which usually feed participants'}"
    )
    if places:
        metres, name, _ = places[0]
        sentence = f"The nearest free food to {label} is {name}, {metres} m away"
        if events:
            sentence += f", and there {'is' if n == 1 else 'are'} {hackathons}"
    elif place and OVERPASS not in failed and GEOCODE not in failed:
        sentence = (
            f"I found no community fridges or gurdwaras within 2 km of {label}, "
            f"but there {'is' if n == 1 else 'are'} {hackathons}"
        )
    else:
        sentence = f"There {'is' if n == 1 else 'are'} {hackathons}"
        if not place:
            sentence += "; tell me an area to find community fridges and gurdwaras too"
    if failed:
        sentence += f" (I could not reach {_join(failed)} this time)"
    return sentence + "."
