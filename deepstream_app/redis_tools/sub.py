import redis
import json
from typing import Dict, Optional
import threading

class RedisSubscriber:
    def __init__(self, data_store: Dict[str, Dict]) -> None:
        self._r = redis.Redis(host="localhost", port=6379, decode_responses=True)
        self._pubsub = self._r.pubsub()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.data_store = data_store  # reference to manager's store

    def subscribe(self, channel: str):
        self._pubsub.subscribe(channel)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_thread(self):
        if not self.is_running():
            self._stop.clear()
            self._thread = threading.Thread(target=self._listen, daemon=True)
            self._thread.start()

    def _listen(self):
        for raw_data in self._pubsub.listen():
            if self._stop.is_set():
                break
            if raw_data["type"] != "message":
                continue
            try:
                data = json.loads(raw_data["data"])
            except json.JSONDecodeError:
                continue
            channel = raw_data["channel"]
            self.data_store[channel] = data

    def stop_thread(self):
        self._stop.set()
        if self._pubsub:
            self._pubsub.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.1)
