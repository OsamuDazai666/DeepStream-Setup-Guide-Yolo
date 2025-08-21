from websockets.sync.client import connect
import time


class WebSocketClient:
    def __init__(self, uri="ws://localhost:5000"):
        self.uri = uri
        self.websocket = None

    def connect(self):
        """Establish connection"""
        self.websocket = connect(self.uri)
        print(f"Connected to {self.uri}")

    def ping(self, message: str="ping"):
        """Send a message"""
        if not self.websocket:
            raise ConnectionError("Not connected to server")
        self.websocket.send(message)
        print(f">>> {message}")

    def close(self):
        """Close connection"""
        if self.websocket:
            self.websocket.close()
            print("Connection closed")


