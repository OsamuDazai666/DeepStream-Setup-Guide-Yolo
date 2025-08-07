#!/usr/bin/env python3
# rtsp_windows.py
import cv2
import threading

BASE_PORT = 8555
NUM_CAMS  = 2
WINDOW_PREFIX = "cam"

def open_stream(cam_id):
    url = f"rtsp://localhost:{BASE_PORT + cam_id}/ds-test"
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    win = f"{WINDOW_PREFIX}{cam_id}"
    while True:
        ok, frame = cap.read()
        if not ok:                 # show placeholder on failure
            frame = 128 * np.ones((360, 640, 3), dtype=np.uint8)
            cv2.putText(frame, f"CAM{cam_id} DOWN", (20, 180),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        cv2.imshow(win, frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    cap.release()

if __name__ == "__main__":
    import numpy as np
    threads = [threading.Thread(target=open_stream, args=(i,), daemon=True)
               for i in range(NUM_CAMS)]
    for t in threads: t.start()
    # keep main alive
    try:
        while True:
            if cv2.waitKey(50) & 0xFF == ord('q'):
                break
    finally:
        cv2.destroyAllWindows()