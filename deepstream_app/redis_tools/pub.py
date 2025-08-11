#!/usr/bin/env python3
import json
import redis
from typing import Dict

class RedisPublisher:
    def __init__(self, channel: str = "channel:1") -> None:
        self.channel = channel
        self.r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

    def publish(self, data: Dict):
        self.r.publish(self.channel, json.dumps(data))
