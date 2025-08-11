#!/usr/bin/env python3
import json
import redis
from typing import Dict

class RedisPublisher:
    def __init__(self) -> None:
        self.channel = "demo:events"
        self.r = redis.Redis(host="localhost", port=6379, decode_responses=True)

    def publish(self, data: Dict):
        self.r.publish(self.channel, json.dumps(data))
