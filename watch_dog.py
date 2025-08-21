# import asyncio
# import time
# from websockets.asyncio.server import serve

# last_beat_time = time.time()

# async def restart_program():
#     print("🔄 Restarting program... (put your restart logic here)")
#     # Example: subprocess.run(["python3", "your_program.py"])

# async def heartbeat_monitor():
#     """Background task that checks if pings stopped."""
#     global last_beat_time
#     while True:
#         await asyncio.sleep(1)  # check every second
#         if time.time() - last_beat_time >= 5:  # 5 seconds without ping
#             print("❌ Program has stopped")
#             await restart_program()
#             last_beat_time = time.time()  # reset after restart

# async def handler(websocket):
#     """Handles incoming WebSocket messages."""
#     global last_beat_time
#     async for message in websocket:
#         if message == "ping":
#             print("✅ Program is working")
#             last_beat_time = time.time()

# async def main():
#     # Start the watchdog task in the background
#     asyncio.create_task(heartbeat_monitor())

#     async with serve(handler, "localhost", 5000) as server:
#         print("Watchdog server running on ws://localhost:5000")
#         await server.serve_forever()

# if __name__ == "__main__":
#     asyncio.run(main())


#!/usr/bin/env python3
import asyncio
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# ---- logging setup -------------------------------------------------
LOG_DIR   = Path(__file__).with_suffix('')    # ./watchdog
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE  = LOG_DIR / "watchdog.log"
MAX_BYTES = 5 * 1024 * 1024   # 5 MiB
BACKUPS   = 2                 # keep watchdog.log, watchdog.log.1, watchdog.log.2

handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUPS
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[handler, logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("watchdog")

# ---- the rest of the original code, adapted ------------------------
last_beat_time = time.time()

async def restart_program():
    import psutil  # still needed, pip install psutil
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        if p.info["cmdline"] and "main.py" in " ".join(p.info["cmdline"]):
            log.info("🪓  Killing PID %s", p.info["pid"])
            os.kill(p.info["pid"], signal.SIGKILL)

    log_file = LOG_DIR / "main.log"
    with log_file.open("ab") as lf:        # open in append-binary mode
        subprocess.Popen(
            [sys.executable, "deepstream_app/main.py"],
            stdout=lf,
            stderr=subprocess.STDOUT
        )

async def heartbeat_monitor():
    global last_beat_time
    while True:
        await asyncio.sleep(1)
        if time.time() - last_beat_time >= 30:
            log.warning("❌ Program has stopped")
            await restart_program()
            last_beat_time = time.time()

async def handler(websocket):
    global last_beat_time
    try:
        async for message in websocket:
            if message == "ping":
                log.debug("✅ Program is working")
                last_beat_time = time.time()
    except Exception as e:
        print(f"Error: {e}")

async def main():
    asyncio.create_task(heartbeat_monitor())
    from websockets.asyncio.server import serve
    async with serve(handler, "localhost", 5000) as server:
        log.info("Watchdog server running on ws://localhost:5000")
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
