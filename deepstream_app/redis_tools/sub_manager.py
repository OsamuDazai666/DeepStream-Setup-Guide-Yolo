from .sub import RedisSubscriber
from typing import Dict

class RedisSubscriberManager:
    def __init__(self) -> None:
        # Map of channel → latest message
        self.data_store: Dict[str, Dict] = {}
        self.subscriber = RedisSubscriber(self.data_store)

    def get_data(self, source_id) -> Dict:
        channel_id = f"stream:{source_id}"

        # Subscribe to new channel if not already subscribed
        if channel_id not in self.data_store:
            self.data_store[channel_id] = None
            self.subscriber.subscribe(channel_id)

        # Start the listener if not running
        if not self.subscriber.is_running():
            self.subscriber.start_thread()

        return self.data_store[channel_id]
