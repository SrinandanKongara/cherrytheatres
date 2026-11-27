# CherryTheatres cloud worker

Tracks Avengers: Doomsday and Dune: Part Three at AAA Cinemas, Prasads Multiplex, AMB Cinemas, ART Cinemas, Allu Cinemas, and Aparna Cinemas (Shamshabad). The worker reads each venue's listings and opens the standard BookMyShow accessibility seat table in Chromium for discovered sessions with an event code. It visits a fresh page each cycle and never clicks a seat.

Screen alerts give the screen inventory and all showtimes for a newly listed screen. Seat alerts identify individual seats and rows that became selectable. BookMyShow's “Booked” label does not distinguish a reservation from a hold or a blocked seat. Initial snapshots establish a baseline. A failed fetch or incomplete seat scan keeps the previous baseline.

## Deploy

1. Deploy this directory as a Docker background worker with a single instance and a persistent disk mounted at /data. A Render background worker with a persistent disk requires a paid plan.
2. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the host's secret settings. The chat ID is in the existing relay's telegram_chat_id.txt. Use the current bot token and keep it out of source control. The worker sends directly to the registered chat and does not use Telegram getUpdates.
3. BMS_DATES defaults to 16–23 December 2026. Override it with comma-separated ISO dates. POLL_SECONDS defaults to 120 for the first validation. The worker pauses on HTTP 403/429 and waits at least 15 seconds between cycles. A 15-second full cycle across every venue, date, and seat map has not been established.
4. On the host, run python worker.py --probe to check the known Prasads Screen 4 seat map. Run python worker.py --test-telegram to confirm delivery, then start the standard worker command. Check the logs for successful venue and seat scans. Some sessions may lack an event code, so confirm seat coverage independently of listings.
5. Restart the worker and verify it reuses tracker.sqlite3. Once it works on the host, the laptop and local extension can be closed.

The worker writes snapshots and queued alerts in the same SQLite transaction. A Telegram failure leaves messages queued. If the worker exits between Telegram acceptance and marking a message delivered, the alert may repeat. Use only one worker instance.
