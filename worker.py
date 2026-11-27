import argparse
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BMS = "https://in.bookmyshow.com"
VENUES = {
    "ACAA": "AAA Cinemas", "PRHN": "Prasads Multiplex", "AMBH": "AMB Cinemas",
    "ACEV": "ART Cinemas", "ALUC": "Allu Cinemas", "APNS": "Aparna Cinemas, Shamshabad",
}
MOVIES = {"avengers doomsday": "Avengers: Doomsday", "dune part three": "Dune: Part Three"}
TEST_SHOW = {"event": "ET00526390", "venue": "PRHN", "session": "259268",
             "date": "20261217", "movie": "Dune: Part Three", "screen": "Screen 4", "time": "06:00 PM"}


def read_json(url, payload=None):
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers),
                                timeout=12) as response:
        return json.load(response)


def parse_shows(payload):
    records = {}

    def walk(node, context):
        if isinstance(node, list):
            for item in node:
                walk(item, context)
            return
        if not isinstance(node, dict):
            return
        event = node.get("Event")
        event = event if isinstance(event, dict) else {}
        context = dict(context)
        fields = {
            "movie": ("EventName", "EventTitle"),
            "event": ("EventCode", "EventId"),
            "screen": ("ScreenName",),
        }
        for field, keys in fields.items():
            value = next((source[key] for source in (node, event)
                          for key in keys if isinstance(source.get(key), str) and source[key]), None)
            if value:
                context[field] = value
        if node.get("ShowTime"):
            if not context.get("movie"):
                raise ValueError("Showtime has no movie title")
            title = context["movie"].lower().replace(":", " ").strip()
            movie = MOVIES.get(" ".join(title.split()))
            if movie:
                session = str(node.get("SessionId") or "")
                if not session.isdigit() or not context.get("screen"):
                    raise ValueError("Tracked show is missing a session ID or screen")
                records[session] = {**context, "movie": movie, "session": session,
                                    "time": str(node["ShowTime"])}
        for value in node.values():
            walk(value, context)

    if not isinstance(payload, dict) or not {"ShowDetails", "ShowDatesArray"} & payload.keys():
        raise ValueError("Unrecognized BookMyShow showtime response")
    if payload.get("ShowDetails") is None:
        raise ValueError("BookMyShow response has no ShowDetails")
    walk(payload["ShowDetails"], {})
    return records


def listing_changes(old, new):
    lines = []
    for title in MOVIES.values():
        before = {k: v for k, v in old.items() if v["movie"] == title}
        after = {k: v for k, v in new.items() if v["movie"] == title}
        old_screens = {s["screen"] for s in before.values()}
        screens = {s["screen"] for s in after.values()}
        details = []
        for screen in sorted(screens - old_screens):
            times = [s["time"] for s in after.values() if s["screen"] == screen]
            details.append(f"Screen added · {screen} · {', '.join(times)}")
        for screen in sorted(old_screens - screens):
            details.append(f"Screen removed · {screen}")
        for key in sorted(before.keys() | after.keys()):
            left, right = before.get(key), after.get(key)
            if left and (not right or (left["time"], left["screen"]) != (right["time"], right["screen"])):
                if left["screen"] in screens:
                    details.append(f"Show removed · {left['screen']} · {left['time']}")
            if right and (not left or (left["time"], left["screen"]) != (right["time"], right["screen"])):
                if right["screen"] in old_screens:
                    details.append(f"Show added · {right['screen']} · {right['time']}")
        if details:
            lines.extend([title, f"{len(screens)} listed screens · {', '.join(sorted(screens)) or 'none'}", *details])
    return lines


def seat_changes(old, new):
    lines = []
    for row, seats in new.items():
        prior = old.get(row)
        if not prior:
            continue
        opened = [label for label, status in seats.items()
                  if status == "available" and prior.get(label) == "booked"]
        if opened and prior.keys() == seats.keys() and set(prior.values()) == {"booked"}:
            lines.append(f"Row became available · {row}")
        if opened:
            lines.append(f"{row} · seats now available · {', '.join(opened)}")
    return lines


def read_seats(page, show):
    route = "/".join(show[key] for key in ("event", "venue", "session", "date"))
    url = f"{BMS}/movies/HYD/seat-layout/{route}"
    response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
    if response is None or response.status >= 400:
        raise ValueError(f"Seat page HTTP {response.status if response else 'unknown'}")
    if urllib.parse.urlsplit(page.url).path != urllib.parse.urlsplit(url).path:
        raise ValueError("Seat page redirected")
    page.get_by_role("button", name="Open accessibility seat selection").click(timeout=8000)
    page.locator("#quantity-select").select_option(label="1 Ticket", timeout=5000)
    category = page.locator("#category-select")
    row_select = page.locator("#row-select")
    categories = category.locator("option").evaluate_all(
        "(options) => options.filter(o => o.value && !o.disabled).map(o => [o.value, o.text])"
    )
    rows = {}
    for value, label in categories:
        category.select_option(value, timeout=5000)
        row_select.locator("option:not([disabled])").filter(has_text="Row").first.wait_for(
            state="attached", timeout=5000
        )
        options = row_select.locator("option").evaluate_all(
            "(options) => options.filter(o => o.value && !o.disabled && /Row /i.test(o.text)).map(o => [o.value,o.text])"
        )
        for row_value, row_label in options:
            row_select.select_option(row_value, timeout=5000)
            row_name = re.escape(row_label.split()[-1])
            grid = page.get_by_role("grid", name=re.compile(rf"^Seats for Row.*(?:-| ){row_name}$", re.I))
            grid.wait_for(state="visible", timeout=5000)
            buttons = grid.get_by_role("button").evaluate_all(
                "(buttons) => buttons.map(b => [b.getAttribute('aria-label'),b.textContent.trim()])"
            )
            seats = {}
            for aria, text in buttons:
                match = re.search(r"\bseat (\S+) in row\b", aria or "", re.I)
                if not match or text not in {"Select", "Booked"}:
                    raise ValueError("Unrecognized seat status or label")
                seats[match[1]] = "available" if text == "Select" else "booked"
            if not seats or len(seats) != len(buttons):
                raise ValueError("Empty or duplicate seat data")
            rows[f"{label} / {row_label}"] = seats
    if not rows:
        raise ValueError("Empty accessibility seat map")
    return rows


def telegram(message):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    try:
        result = read_json(f"https://api.telegram.org/bot{token}/sendMessage",
                           {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": message})
        if not result.get("ok"):
            raise RuntimeError("Telegram rejected the message")
    except Exception:
        raise RuntimeError("Telegram delivery failed. Check the host secrets and bot chat.") from None


def record(db, key, current, compare, heading):
    previous = db.execute("SELECT data FROM snapshots WHERE key = ?", (key,)).fetchone()
    changes = compare(json.loads(previous[0]), current) if previous else []
    with db:
        db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?)", (key, json.dumps(current)))
        if changes:
            body = heading
            for line in changes:
                if len(body) + len(line) > 3000:
                    db.execute("INSERT INTO outbox (body) VALUES (?)", (body,))
                    body = heading
                body += "\n" + line[:2000]
            db.execute("INSERT INTO outbox (body) VALUES (?)", (body,))


def flush(db):
    for entry, message in db.execute("SELECT id, body FROM outbox ORDER BY id LIMIT 20").fetchall():
        telegram(message)
        with db:
            db.execute("DELETE FROM outbox WHERE id = ?", (entry,))


def run(interval, data_dir):
    from playwright.sync_api import sync_playwright

    os.environ["TELEGRAM_BOT_TOKEN"]
    os.environ["TELEGRAM_CHAT_ID"]
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "tracker.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS snapshots (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    dates = [dt.date.fromisoformat(date.strip()) for date in os.getenv(
        "BMS_DATES", ",".join(f"2026-12-{day}" for day in range(16, 24))
    ).split(",")]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        while True:
            started = time.monotonic()
            now = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
            shows = {}
            for date in dates:
                if date < now:
                    continue
                code = date.strftime("%Y%m%d")
                for venue, name in VENUES.items():
                    try:
                        params = urllib.parse.urlencode(dict(venueCode=venue, dateCode=code, regionCode="HYD"))
                        rows = parse_shows(read_json(f"{BMS}/api/v3/mobile/showtimes/byvenue?{params}"))
                        logging.info("Showtimes %s %s · %d tracked", venue, code, len(rows))
                        record(db, f"list:{venue}:{code}", rows, listing_changes,
                               f"CherryTheatres · {name} · {date.isoformat()}")
                        for session, show in rows.items():
                            event = show.get("event", "")
                            if (venue, code, session) == ("PRHN", "20261217", "259268"):
                                event = TEST_SHOW["event"]
                            if event.startswith("ET") and event[2:].isdigit():
                                shows[venue, code, session] = {**show, "venue": venue, "date": code}
                    except urllib.error.HTTPError as error:
                        logging.warning("Showtimes %s %s HTTP %s", venue, code, error.code)
                        if error.code in {403, 429}:
                            logging.warning("Pausing after BookMyShow restricted venue polling")
                            time.sleep(max(300, interval))
                            break
                    except Exception as error:
                        logging.warning("Showtimes %s %s skipped · %s", venue, code, type(error).__name__)
                else:
                    continue
                break
            else:
                for key, show in shows.items():
                    try:
                        rows = read_seats(page, show)
                        logging.info("Seat map %s · %d rows", key, len(rows))
                        record(db, "seat:" + ":".join(key), rows, seat_changes,
                               f"CherryTheatres · {show['movie']}\n{VENUES[show['venue']]} · "
                               f"{show['screen']} · {show['date']} · {show['time']}")
                    except Exception as error:
                        logging.warning("Seat map %s skipped · %s", key, str(error).splitlines()[0])
                        if "HTTP 403" in str(error) or "HTTP 429" in str(error):
                            time.sleep(max(300, interval))
                            break
            try:
                flush(db)
            except RuntimeError as error:
                logging.warning("%s Queued alerts will retry.", error)
            logging.info("Cycle finished in %.1fs (%d seat sessions discovered)", time.monotonic() - started, len(shows))
            time.sleep(max(15, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", help="Read only the known Screen 4 seat map")
    parser.add_argument("--test-telegram", action="store_true", help="Send one test to the configured chat")
    args = parser.parse_args()
    if args.test_telegram:
        telegram("CherryTheatres cloud worker · Telegram test")
    elif args.probe:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            rows = read_seats(browser.new_page(), TEST_SHOW)
            print(json.dumps(rows, indent=2))
    else:
        run(max(15, int(os.getenv("POLL_SECONDS", "120"))), Path(os.getenv("DATA_DIR", "/data")))
