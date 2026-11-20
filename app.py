"""Free Food London: where a student can get free food near a London place.

One outcome, live sources, no API keys:

1. Geocode the place: postcodes.io for a postcode or outcode, otherwise
   Nominatim (OpenStreetMap's geocoder), held to a London bounding box.
2. Overpass (OpenStreetMap): community fridges and food-sharing points
   (amenity=food_sharing, amenity=give_box) and gurdwaras, whose langar is a
   free vegetarian meal open to everyone, within about 2 km. overpass-api.de
   first, then a mirror. If neither answers in time, the saved Greater London
   snapshot in osm_london.json (real OpenStreetMap data, dated) is used.
3. Devpost: upcoming in-person hackathons whose location mentions London.

Every source runs in its own thread with a ~6 s timeout, and the whole call
answers within ~12 s. If a source fails, the others are still returned, and
`sources`, `note` and the narrative say which.

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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

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
SNAPSHOT_PATH = Path(__file__).with_name("osm_london.json")

#: Nominatim and Overpass ask for a descriptive User-Agent; urllib sends none.
USER_AGENT = "pippa-agent-app-free-food-london/0.2.1 (https://github.com/Pippa-Labs)"

#: Per-request timeout for each source, in seconds.
TIMEOUT = 6.0
#: The whole call answers within this many seconds (Pippa stops it at 20).
BUDGET = 12.0
#: Ask the Overpass mirror too if the main server has not answered by then.
MIRROR_AFTER = 3.0
OVERPASS_HOSTS = ("overpass-api.de", "overpass.kumi.systems")
RADIUS_M = 2000
MAX_PLACES = 5
MAX_EVENTS = 3
DEFAULT_DAYS = 14

LONDON_TZ = ZoneInfo("Europe/London")
#: Charing Cross, where distances are measured from when no area is named.
CENTRAL_LONDON = (51.5074, -0.1278)
#: Greater London, with a small margin: south, north, west, east.
LONDON_BOX = (51.28, 51.70, -0.52, 0.34)

POSTCODE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}$")
OUTCODE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?$")

#: Anything mentioning these is need-based food aid, never returned.
NEED_BASED = ("food bank", "foodbank", "soup kitchen")

FRIDGE_TIP = "tip: take what you'll use"
LANGAR_TIP = "tip: cover your head, shoes off, donations welcome"
HACKATHON = "hackathon (usually fed)"

#: Langar is Punjabi vegetarian food. Not vegan: it is cooked with dairy.
LANGAR_CUISINES = {"indian", "asian", "south asian", "punjabi", "vegetarian", "veggie", "veg"}
#: Words that count as a match for a cuisine in an event's title or text.
CUISINE_WORDS = {
    "asian": ["asian", "chinese", "japanese", "korean", "thai", "vietnamese",
              "indian", "sushi", "ramen", "dumplings?", "dim sum", "curry", "boba"],
    "indian": ["indian", "curry", "biryani", "samosas?", "punjabi", "langar"],
    "vegetarian": ["vegetarian", "veggie", "vegan", "plant[- ]based"],
    "veggie": ["vegetarian", "veggie", "vegan", "plant[- ]based"],
    "vegan": ["vegan", "plant[- ]based"],
    "pizza": ["pizzas?"],
}
#: Common misspellings, read as the cuisine they were meant to be.
CUISINE_ALIASES = {"piazza": "pizza", "pizzas": "pizza", "pitza": "pizza", "vegitarian": "vegetarian"}

GEOCODE = "the place lookup (postcodes.io / Nominatim)"
OVERPASS = "OpenStreetMap (Overpass)"
DEVPOST = "Devpost"

#: What a source thread may fail with (TimeoutError includes a thread that
#: did not finish in time). A failed source never sinks the call: it is named
#: in `sources` and the narrative, and the others still answer.
SOURCE_ERRORS = (TimeoutError, ValueError, KeyError, TypeError, AttributeError)


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


SOURCE_ERRORS = (SourceFailed, *SOURCE_ERRORS)


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
        if call.outcome_id != "freefood.near":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        place = _clean(call.inputs.get("location"))
        when = _clean(call.inputs.get("when"))
        cuisine = _clean(call.inputs.get("cuisine"))
        today = call.context.now.astimezone(LONDON_TZ).date()
        start, end, period, understood = _window(when, today)

        deadline = time.monotonic() + BUDGET
        failed: list[str] = []
        notes: list[str] = []
        places: list[dict] = []
        label = place
        used_snapshot = False
        saved = _snapshot()
        saved_elements = saved["elements"] if saved else []
        spot = None

        pool = ThreadPoolExecutor(max_workers=4)
        try:
            # Events need no place, so they start at once, alongside the
            # geocode; Overpass starts the moment there are coordinates.
            events_job = pool.submit(self._devpost)

            if place:
                try:
                    spot = pool.submit(self._geocode, place).result(timeout=TIMEOUT + 0.5)
                except SOURCE_ERRORS:
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

            if spot is not None:
                lat, lon, label = spot
                live = self._live_places(pool, lat, lon, deadline)
                if live is not None:
                    places = live
                elif saved:
                    places = _parse_elements(saved_elements, lat, lon)
                    used_snapshot = True
                    notes.append(
                        "The live map was slow, so places come from saved "
                        f"OpenStreetMap data from {saved['fetched']}."
                    )
                else:
                    failed.append(OVERPASS)
                if not places and saved_elements:
                    # Nothing mapped within 2 km: the nearest real places,
                    # with their real distance, beat an empty answer.
                    places = _parse_elements(saved_elements, lat, lon, radius=None)
                    used_snapshot = True
                    notes.append(
                        f"Nothing is mapped within 2 km of {label}, so these are "
                        "the nearest mapped places in London."
                    )

            raw_events: list[dict] = []
            try:
                raw_events = events_job.result(timeout=max(0.2, deadline - time.monotonic()))
            except SOURCE_ERRORS:
                failed.append(DEVPOST)
        finally:
            # Never wait for a source that is still hanging: its answer is no
            # longer wanted, and the user is.
            pool.shutdown(wait=False, cancel_futures=True)

        # No place named, or it could not be looked up: all of Greater London,
        # from the saved data, measured from central London.
        area_wide = spot is None and bool(saved_elements)
        if area_wide:
            places = _parse_elements(saved_elements, *CENTRAL_LONDON, radius=None,
                                     origin="central London")
            used_snapshot = True
            if place:
                notes.append(
                    f"I could not look up {place} just now, so these are from all "
                    "of Greater London, with distances from central London."
                )
            else:
                notes.append(
                    "No area was named, so these are from all of Greater London, "
                    "with distances from central London. Name an area for nearby results."
                )

        if not understood:
            notes.append(f"I could not tell when \"{when}\" is, so events are for the next 14 days.")
        events = [e for e in raw_events if _in_window(e, start, end, bool(when))]
        events.sort(key=lambda e: e["start"] or date.max)

        if cuisine:
            places, events = _match_cuisine(cuisine, places, events, notes)

        places = places[:MAX_PLACES]
        events = events[:MAX_EVENTS]

        if not places and not events:
            if failed:
                error = SourcesDown(f"sources failed: {failed}")
                error.user_message = (
                    f"I could not reach {_join(failed)} just now, and found no "
                    f"free food {'near ' + label if label else 'in London'} from "
                    "the rest. Please try again shortly."
                )
                raise error
            if not place:
                raise NeedAPlace(f"no location, and no London events {period}")
            error = NothingFound(f"nothing within {RADIUS_M} m of {label!r}")
            error.user_message = (
                f"I found no community fridges or gurdwaras within 2 km of "
                f"{label}, and no London events with free food {period}. Try a "
                "wider area, like the borough or a bigger town centre nearby, "
                "or a longer time, like \"next month\"."
            )
            raise error

        sources = (
            "Places © OpenStreetMap contributors (ODbL, openstreetmap.org/copyright)"
            + (", from the saved snapshot" if used_snapshot else ", live from the Overpass API")
            + "; locations from postcodes.io and Nominatim. Events from Devpost (devpost.com/hackathons)."
        )
        if failed:
            sources += f" Could not reach this time: {_join(failed)}."

        data = {
            "places": [p["text"] for p in places],
            "events": [_event_text(e) for e in events],
            "sources": sources,
        }
        if notes:
            data["note"] = " ".join(notes)

        return Result(
            data=data,
            narrative=_narrative(place, label, places, events, period, failed, area_wide),
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
            south, north, west, east = LONDON_BOX
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

    def _live_places(self, pool, lat, lon, deadline):
        """Places from the main Overpass server, or its mirror, or None.

        The mirror is asked only once the main server has failed or has not
        answered within MIRROR_AFTER seconds, so a healthy server is never
        asked twice. The first good answer wins.
        """
        pending = {pool.submit(self._overpass, OVERPASS_HOSTS[0], lat, lon)}
        mirror_at = time.monotonic() + MIRROR_AFTER
        mirror = False
        while pending or not mirror:
            now = time.monotonic()
            if now >= deadline:
                return None
            until = deadline if mirror else min(deadline, mirror_at)
            done, pending = wait(pending, timeout=max(0.0, until - now), return_when=FIRST_COMPLETED)
            for job in done:
                try:
                    return job.result()
                except SOURCE_ERRORS:
                    pass
            if not mirror and (not pending or time.monotonic() >= mirror_at):
                pending.add(pool.submit(self._overpass, OVERPASS_HOSTS[1], lat, lon))
                mirror = True
        return None

    def _overpass(self, host: str, lat: float, lon: float) -> list[dict]:
        around = f"(around:{RADIUS_M},{lat:.5f},{lon:.5f})"
        query = (
            "[out:json][timeout:6];("
            f'nwr{around}["amenity"="food_sharing"];'
            f'nwr{around}["amenity"="give_box"];'
            f'nwr{around}["amenity"="place_of_worship"]["religion"="sikh"];'
            ");out center 20;"
        )
        data = self._get_json(f"https://{host}/api/interpreter?" + urlencode({"data": query}))
        if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
            raise SourceFailed("Overpass answered without a list of elements")
        if not data["elements"] and data.get("remark"):
            # Overpass reports its own timeouts as a 200 with a remark.
            raise SourceFailed("Overpass gave up on the query")
        return _parse_elements(data["elements"], lat, lon)

    # ── Source 3: Devpost ───────────────────────────────────────────────────

    def _devpost(self) -> list[dict]:
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

        events: list[dict] = []
        for hackathon in data["hackathons"]:
            if not isinstance(hackathon, dict):
                continue
            where = ((hackathon.get("displayed_location") or {}).get("location") or "").strip()
            if not _mentions_london(where):
                continue
            title = (hackathon.get("title") or "").strip()
            link = (hackathon.get("url") or "").strip()
            if not title or not link.startswith("https://"):
                continue
            when = (hackathon.get("submission_period_dates") or "").strip()
            first, last = _devpost_dates(when)
            themes = " ".join(
                t.get("name", "") for t in hackathon.get("themes") or [] if isinstance(t, dict)
            )
            events.append({
                "title": title, "label": HACKATHON, "when": when or "date not given",
                "start": first, "end": last, "where": where, "link": link,
                "source": "Devpost", "text": f"{title} {themes}",
            })
        return events


# ── Places ──────────────────────────────────────────────────────────────────


def _snapshot():
    """The saved Greater London OpenStreetMap data, or None if unreadable."""
    try:
        data = json.loads(SNAPSHOT_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        return None
    return data


def _parse_elements(elements, lat: float, lon: float, radius=RADIUS_M, origin=None) -> list[dict]:
    """Overpass elements to places within `radius` m (None: any), nearest first.

    `origin` names where distances are measured from, when that is not the
    place the user asked about.
    """
    best: dict[tuple[str, str], dict] = {}
    for element in elements:
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
        exact = _distance_m(lat, lon, float(plat), float(plon))
        if radius is not None and exact > radius:
            continue
        metres = int(round(exact / 10) * 10)
        label, tip, unnamed = kind
        name = (tags.get("name") or tags.get("name:en") or unnamed).strip()
        parts = [name, label, f"{metres} m" + (f" from {origin}" if origin else "")]
        if tags.get("opening_hours"):
            parts.append(f"open {tags['opening_hours'].strip()}")
        parts.append(tip)
        place = {
            "metres": metres, "name": name, "langar": tip == LANGAR_TIP,
            "text": ", ".join(parts) + " (source: OpenStreetMap)",
        }
        # A building is often mapped twice (as a point and an outline).
        key = (name.lower(), label)
        if key not in best or metres < best[key]["metres"]:
            best[key] = place
    return sorted(best.values(), key=lambda p: (p["metres"], p["name"]))


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


# ── Cuisine ─────────────────────────────────────────────────────────────────


def _match_cuisine(cuisine: str, places: list[dict], events: list[dict], notes: list[str]):
    """Keep what matches the cuisine; if nothing does, keep everything and say so."""
    wanted = re.sub(r"\s+(food|meals?|dishes)$", "", cuisine.lower()).strip()
    wanted = CUISINE_ALIASES.get(wanted, wanted)
    terms = CUISINE_WORDS.get(wanted, [re.escape(wanted)])
    pattern = re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)

    good_places = [p for p in places if p["langar"] and wanted in LANGAR_CUISINES]
    good_events = [e for e in events if pattern.search(e["text"])]

    if not good_places and not good_events:
        if places or events:
            notes.append(f"Nothing matched {cuisine}, so here is everything else.")
        return places, events

    if good_places:
        notes.append(f"Langar at a gurdwara is Punjabi vegetarian food, so it fits {cuisine}.")
    missed = [name for name, have, good in (("places", places, good_places),
                                            ("events", events, good_events)) if have and not good]
    if missed:
        notes.append(f"No {' or '.join(missed)} matched {cuisine}, so all of those are shown.")
    return (good_places or places), (good_events or events)


# ── Events and dates ────────────────────────────────────────────────────────

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

DEVPOST_DATES = re.compile(
    r"^\s*([A-Za-z]{3})[a-z]*\s+(\d{1,2})(?:,\s*(\d{4}))?"
    r"(?:\s*-\s*(?:([A-Za-z]{3})[a-z]*\s+)?(\d{1,2})(?:,\s*(\d{4}))?)?\s*$"
)


def _devpost_dates(text: str):
    """(start, end) from "Nov 20 - 22, 2026" and its variants, or (None, None)."""
    m = DEVPOST_DATES.match(text or "")
    if not m:
        return None, None
    m1, d1, y1, m2, d2, y2 = m.groups()
    month1 = MONTHS.get(m1.lower())
    month2 = MONTHS.get(m2.lower()) if m2 else month1
    year2 = int(y2) if y2 else (int(y1) if y1 else None)
    if not month1 or not month2 or year2 is None:
        return None, None
    year1 = int(y1) if y1 else (year2 - 1 if month1 > month2 else year2)
    try:
        first = date(year1, month1, int(d1))
        last = date(year2, month2, int(d2)) if d2 else first
    except ValueError:
        return None, None
    return first, last


def _window(text: str, today: date):
    """(start, end, words for it, understood) for what the user said."""
    default = (today, today + timedelta(days=DEFAULT_DAYS - 1), "in the next 14 days")
    t = re.sub(r"[^a-z0-9 /-]", " ", (text or "").lower())
    t = " ".join(t.split())
    t = re.sub(r"^(on|for|in|over|during|within|by)\s+", "", t)
    if not t:
        return (*default, True)

    def weekend(weeks_ahead: int):
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() == 6:  # Sunday: this weekend is today
            saturday = today - timedelta(days=1)
        saturday += timedelta(weeks=weeks_ahead)
        return max(saturday, today), saturday + timedelta(days=1)

    if t in ("today", "tonight", "this evening"):
        return today, today, "today", True
    if t == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=1), "tomorrow", True
    if t in ("this weekend", "weekend", "the weekend"):
        return (*weekend(0), "this weekend", True)
    if t == "next weekend":
        return (*weekend(1), "next weekend", True)
    if t == "this week":
        return today, today + timedelta(days=6 - today.weekday()), "this week", True
    if t == "next week":
        monday = today + timedelta(days=7 - today.weekday())
        return monday, monday + timedelta(days=6), "next week", True
    if t == "this month":
        following = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        return today, following - timedelta(days=1), "this month", True
    if t in ("next month", "a month", "the next month"):
        return today, today + timedelta(days=30), "in the next month", True
    if t in ("next few days", "the next few days", "few days", "a few days"):
        return today, today + timedelta(days=2), "in the next few days", True

    m = re.fullmatch(r"(?:the )?(?:next |coming )?(\d{1,2}) (days?|weeks?)", t)
    if m and int(m.group(1)) >= 1:
        n = int(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
        return today, today + timedelta(days=n - 1), f"in the next {m.group(1)} {m.group(2)}", True

    m = re.fullmatch(r"(this |next |coming |on )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?", t)
    if m:
        day = today + timedelta(days=(WEEKDAYS.index(m.group(2)) - today.weekday()) % 7)
        if m.group(1) == "next " and day == today:
            day += timedelta(days=7)
        return day, day, f"on {m.group(2).capitalize()} {day.day} {day.strftime('%B')}", True

    for fmt in ("%Y-%m-%d", "%d %B", "%d %b", "%B %d", "%b %d", "%d/%m/%Y", "%d/%m"):
        try:
            parsed = datetime.strptime(t, fmt).date()
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
        return parsed, parsed, f"on {parsed.day} {parsed.strftime('%B')}", True

    return (*default, False)


def _in_window(event: dict, start: date, end: date, asked: bool) -> bool:
    if event["start"] is None:
        return not asked  # an undated event only when no time was asked for
    return event["start"] <= end and (event["end"] or event["start"]) >= start


def _mentions_london(where: str) -> bool:
    lowered = where.lower()
    # "London" also matches London, Ontario.
    return "london" in lowered and "ontario" not in lowered and "canada" not in lowered


def _event_text(e: dict) -> str:
    return f"{e['title']}, {e['label']}, {e['when']}, {e['where']}, {e['link']} (source: {e['source']})"


# ── Words ───────────────────────────────────────────────────────────────────


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


def _narrative(place, label, places, events, period, failed, area_wide=False) -> str:
    """One plain sentence, never empty."""
    n = len(events)
    found = f"{n} London event{'s' if n != 1 else ''} with likely free food {period}"
    if places and area_wide:
        first = places[0]
        sentence = (
            f"Here is free food across Greater London, starting with {first['name']}, "
            f"{first['metres']} m from central London"
        )
        if events:
            sentence += f", and there {'is' if n == 1 else 'are'} {found}"
        if not place:
            sentence += "; name an area for nearby results"
    elif places:
        first = places[0]
        sentence = f"The nearest free food to {label} is {first['name']}, {first['metres']} m away"
        if events:
            sentence += f", and there {'is' if n == 1 else 'are'} {found}"
    elif place and OVERPASS not in failed and GEOCODE not in failed:
        sentence = (
            f"I found no community fridges or gurdwaras within 2 km of {label}, "
            f"but there {'is' if n == 1 else 'are'} {found}"
        )
    else:
        sentence = f"There {'is' if n == 1 else 'are'} {found}"
        if not place:
            sentence += "; tell me an area to find community fridges and gurdwaras too"
    if failed:
        sentence += f" (I could not reach {_join(failed)} this time)"
    return (sentence or "Here is free food in London") + "."
