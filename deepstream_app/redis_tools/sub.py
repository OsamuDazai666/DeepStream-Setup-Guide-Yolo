from optparse import Option
import redis
import json
from typing import Dict, Optional
import threading

class RedisSubscriber:
    def __init__(self) -> None:
        self._r = redis.Redis(host="localhost", port=6379, decode_responses=True)
        self._pubsub = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.data: Dict | None = None

    def subscribe(self, channel):
        self._pubsub = self._r.pubsub()
        self._pubsub.subscribe(channel)

    def start_thread(self):
        self._thread = threading.Thread(target=self._get_data, daemon=True)
        self._thread.start()

    def _get_data(self):
        for raw_data in self._pubsub.listen():
            if raw_data["type"] != "message":
                continue                   # skip subscribe/unsubscribe events
            self.data = json.loads(raw_data["data"])

    def stop_thread(self):
        if self._pubsub:
            self._pubsub.close()
        self._thread.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.1)

            
