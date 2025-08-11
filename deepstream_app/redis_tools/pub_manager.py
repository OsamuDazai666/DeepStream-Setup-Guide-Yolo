from .pub import RedisPublisher

class RedisPublisherManager:
    def __init__(self) -> None:
        self.publishers = {}

    def publish_data(self, source_id:str, data):
        channel = f"stream:{source_id}"
        if channel not in self.publishers.keys():
            self.publishers[channel] = RedisPublisher(channel=channel)
        self.publishers[channel].publish(data)

        
