# rtsp_viewer.py

import cv2

# Replace this with your RTSP stream URL
RTSP_URL = "rtsp://localhost:8555/ds-test"

def main():
    cap = cv2.VideoCapture(RTSP_URL)

    if not cap.isOpened():
        print("Error: Cannot open RTSP stream.")
        return

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Failed to read frame from stream.")
            break

        cv2.imshow("RTSP Stream", frame)

        # Exit on 'q' key press
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
