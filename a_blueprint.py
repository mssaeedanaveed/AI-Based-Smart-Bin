

import cv2
import time
import json
import base64
import threading
import os
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

from flask import Blueprint, jsonify, session

import firebase_admin
from firebase_admin import credentials, db

load_dotenv()

agent_bp = Blueprint('agent_bp', __name__, url_prefix='/admin/agent')

# -----------------------------
# AI CONFIG
# -----------------------------
API_KEY = os.environ.get("GEMINI_API_KEY")

MODEL_NAME = "gemini-2.5-flash"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY}"
HEADERS = {"Content-Type": "application/json"}

# -----------------------------
# FIREBASE CONFIG
# -----------------------------
SERVICE_ACCOUNT_PATH = os.environ.get("FIREBASE_CREDENTIALS_PATH")
FIREBASE_DATABASE_URL = os.environ.get("FIREBASE_DATABASE_URL")

FIREBASE_NODE = "metal"
ORGANIC_VALUE = 3
PLASTIC_VALUE = 4

# Agar app.py pehle se firebase_admin.initialize_app() kar chuka hai
# (jo ke is project mein hota hai), to yahan dobara init nahi hoga —
# wahi connection reuse hoga. Warna (standalone run ki soorat mein)
# khud initialize kar lega.
try:
    firebase_admin.get_app()
except ValueError:
    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred, {
        "databaseURL": FIREBASE_DATABASE_URL
    })

# -----------------------------
# AI PROMPT
# -----------------------------
PROMPT = """
You are a smart dustbin food and plastic sorting assistant.

Analyze the webcam image and classify the visible item.

Main categories:
1) organic
2) plastic
3) unknown

Organic means:
- Any food item
- Fresh food
- Rotten food
- Spoiled food
- Old food
- Decayed food
- Gala sra food
- Fruit
- Vegetables
- Rice
- Bread
- Roti
- Meat
- Fish
- Eggs
- Cooked food
- Leftover food
- Food waste
- Tea leaves
- Leaves or biodegradable natural waste

Plastic means:
- Plastic bottle
- Plastic bag
- Plastic wrapper
- Chips packet
- Biscuit packet
- Plastic food packaging
- Plastic cup
- Plastic plate
- Plastic spoon
- Plastic fork
- Plastic straw
- Plastic cap
- Polythene bag
- Plastic container
- Any visible plastic item

Unknown means:
- Image is blurry
- Item is not clearly visible
- Camera is too far
- Light is poor
- No clear object is visible
- The item is not food and not plastic

Rules:
- Be conservative.
- If the item is food, classify it as organic even if it is rotten, spoiled, old, or decayed.
- If the item is gala sra food, classify it as organic.
- If any food item is visible, classify it as organic.
- If plastic item or plastic packaging is visible, classify it as plastic.
- If both food and plastic are visible, choose the item that is more clearly visible and dominant in the image.
- If you are not sure, classify it as unknown.
- Return ONLY valid JSON.
- Do not return markdown.
- Do not write explanation outside JSON.

JSON format:
{
  "item_present": "yes|no|unknown",
  "detected_item": "short item name or unknown",
  "category": "organic|plastic|unknown",
  "confidence": 0-100,
  "reason": "one short simple reason"
}
"""

# -----------------------------
# JSON CLEAN FUNCTION
# -----------------------------
def extract_json_from_text(text):
    text = text.strip()
    text = text.replace("```json", "").replace("```", "").strip()

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:
        raise ValueError("AI response valid JSON nahi hai")

    json_text = text[start:end + 1]
    return json.loads(json_text)

# -----------------------------
# RESULT CLEAN FUNCTION
# -----------------------------
def normalize_result(result):
    item_present = str(result.get("item_present", "unknown")).lower().strip()
    detected_item = str(result.get("detected_item", "unknown")).strip()
    category = str(result.get("category", "unknown")).lower().strip()
    reason = str(result.get("reason", "unknown")).strip()

    try:
        confidence = int(result.get("confidence", 0))
    except Exception:
        confidence = 0

    confidence = max(0, min(100, confidence))

    if item_present not in ["yes", "no", "unknown"]:
        item_present = "unknown"

    if category not in ["organic", "plastic", "unknown"]:
        category = "unknown"

    if detected_item == "":
        detected_item = "unknown"

    if reason == "":
        reason = "unknown"

    return {
        "item_present": item_present,
        "detected_item": detected_item,
        "category": category,
        "confidence": confidence,
        "reason": reason
    }

# -----------------------------
# FOOD / PLASTIC CHECK FUNCTION
# -----------------------------
def check_item_with_ai(frame):
    try:
        success, buffer = cv2.imencode(
            ".jpg",
            frame,
            [cv2.IMWRITE_JPEG_QUALITY, 85]
        )

        if not success:
            print("Image convert error")
            return None

        encoded_img = base64.b64encode(buffer).decode("utf-8")

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": PROMPT},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": encoded_img
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1
            }
        }

        response = requests.post(URL, headers=HEADERS, json=payload, timeout=60)
        response.raise_for_status()

        api_result = response.json()
        text = api_result["candidates"][0]["content"]["parts"][0]["text"]

        parsed_result = extract_json_from_text(text)
        final_result = normalize_result(parsed_result)

        return final_result

    except Exception as e:
        print("AI Checking Error:", e)
        return None

# -----------------------------
# FIREBASE SEND FUNCTION
# -----------------------------
def send_to_firebase(value):
    try:
        ref = db.reference(FIREBASE_NODE)
        ref.set(value)
        return True, f"/{FIREBASE_NODE} = {value}"
    except Exception as e:
        return False, f"Firebase Error: {e}"

# -----------------------------
# CHECK + SEND FUNCTION
# -----------------------------
def check_and_send(frame):
    result = check_item_with_ai(frame)

    if result is None:
        return None, "AI checking failed"

    category = result["category"]

    if category == "organic":
        ok, message = send_to_firebase(ORGANIC_VALUE)
        if ok:
            print("Organic Food Detected ->", message)
        else:
            print(message)
        return result, message

    elif category == "plastic":
        ok, message = send_to_firebase(PLASTIC_VALUE)
        if ok:
            print("Plastic Item Detected ->", message)
        else:
            print(message)
        return result, message

    else:
        print("Unknown Item - Firebase update nahi hui")
        return result, "Unknown - no Firebase update"


# -----------------------------
# DRAW SCREEN PANEL (live window overlay)
# -----------------------------
def draw_result_panel(frame, result, firebase_status, analyzing):
    x1, y1, x2, y2 = 20, 20, 850, 270
    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 2)

    y = 50
    gap = 30

    cv2.putText(
        frame,
        "Smart Dustbin Agent",
        (30, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.75,
        (0, 255, 255),
        2
    )
    y += gap

    if analyzing:
        status_text = "Status: Checking Item..."
        status_color = (255, 255, 0)
    else:
        status_text = "Status: Agent Ready"
        status_color = (0, 255, 0)

    cv2.putText(
        frame,
        status_text,
        (30, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        status_color,
        2
    )
    y += gap

    if result:
        category = result.get("category", "unknown")
        confidence = result.get("confidence", 0)

        if category == "organic":
            firebase_value = str(ORGANIC_VALUE)
            display_category = "Organic Food"
        elif category == "plastic":
            firebase_value = str(PLASTIC_VALUE)
            display_category = "Plastic Item"
        else:
            firebase_value = "No Send"
            display_category = "Unknown"

        cv2.putText(
            frame,
            f"Item Present: {result.get('item_present', 'unknown')}",
            (30, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 0, 255),
            2
        )
        y += gap

        cv2.putText(
            frame,
            f"Detected Item: {result.get('detected_item', 'unknown')}",
            (30, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 0, 255),
            2
        )
        y += gap

        cv2.putText(
            frame,
            f"Category: {display_category} ({confidence}%)",
            (30, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 0, 255),
            2
        )
        y += gap

        cv2.putText(
            frame,
            f"Firebase /{FIREBASE_NODE}: {firebase_value}",
            (30, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 0, 255),
            2
        )
        y += gap

        cv2.putText(
            frame,
            f"Reason: {result.get('reason', 'unknown')}",
            (30, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 255),
            2
        )

    if firebase_status:
        cv2.putText(
            frame,
            firebase_status[:80],
            (30, 255),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2
        )


# ======================================================================
# BACKGROUND AGENT STATE
# Camera loop yahan thread ke andar chalta hai, AUR live GUI window
# (cv2.imshow) bhi dikhata hai — bilkul original agent.py jaisa.
# ======================================================================
class DustbinAgent:
    def __init__(self, camera_index=1, interval_seconds=15):
        self.camera_index = camera_index
        self.interval_seconds = interval_seconds

        self._lock = threading.Lock()
        self._thread = None
        self._running = False

        self.last_result = None          # last AI classification result (dict)
        self.last_firebase_status = ""   # last firebase write message
        self.last_checked_at = None      # ISO timestamp of last check
        self.error = None                # last fatal error, if any (e.g. camera not found)

    @property
    def running(self):
        return self._running

    def start(self):
        with self._lock:
            if self._running:
                return False  # already running

            self._running = True
            self.error = None
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            return True

    def stop(self):
        with self._lock:
            self._running = False
            return True

    def get_status(self):
        return {
            'running': self._running,
            'last_result': self.last_result,
            'last_firebase_status': self.last_firebase_status,
            'last_checked_at': self.last_checked_at,
            'error': self.error,
        }

    def _run_loop(self):
        cap = cv2.VideoCapture(self.camera_index)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

        if not cap.isOpened():
            self.error = "Camera open nahi ho rahi (camera_index check karein)"
            self._running = False
            return

        print("Agent Start")
        print(f"Har {self.interval_seconds} seconds item checking hogi")
        print("Organic food / gala sra food par Firebase /metal =", ORGANIC_VALUE)
        print("Plastic item par Firebase /metal =", PLASTIC_VALUE)
        print("Live window mein 'Q' dabaakar bhi agent band kar sakte hain")

        last_check_time = 0
        executor = ThreadPoolExecutor(max_workers=1)
        future = None

        try:
            while self._running:
                ret, frame = cap.read()

                if not ret:
                    self.error = "Camera frame read nahi hua"
                    print(self.error)
                    break

                current_time = time.time()

                # Har interval_seconds baad ek AI check background thread
                # (ThreadPoolExecutor) mein bhejo, taake live window kabhi
                # freeze na ho AI response ka wait karte waqt.
                if current_time - last_check_time >= self.interval_seconds and future is None:
                    print("Item Checking Start...")
                    frame_for_ai = frame.copy()
                    future = executor.submit(check_and_send, frame_for_ai)
                    last_check_time = current_time

                if future is not None and future.done():
                    try:
                        result, status = future.result()
                        self.last_result = result
                        self.last_firebase_status = status
                        self.last_checked_at = datetime.now().isoformat()
                    except Exception as e:
                        print("Agent Error:", e)
                        self.last_firebase_status = f"Agent Error: {e}"
                    finally:
                        future = None

                analyzing = future is not None

                # ✅ Live window (jaisa original agent.py mein tha)
                draw_result_panel(frame, self.last_result, self.last_firebase_status, analyzing)
                cv2.imshow("Smart Dustbin Agent", frame)

                key = cv2.waitKey(1) & 0xFF
                if key == ord("q") or key == ord("Q"):
                    print("Q dabaya gaya - Agent band ho raha hai")
                    self._running = False
                    break

        finally:
            cap.release()
            cv2.destroyAllWindows()
            executor.shutdown(wait=False)
            self._running = False
            print("Agent Closed")


# Ek hi global agent instance (single camera, single loop)
agent = DustbinAgent(camera_index=0, interval_seconds=15)


# -----------------------------
# ROUTES
# -----------------------------
def _is_admin():
    return session.get('user_type') == 'admin'


@agent_bp.route('/start', methods=['POST'])
def start_agent():
    if not _is_admin():
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    started = agent.start()
    return jsonify({
        'success': True,
        'started': started,
        'running': agent.running,
        'message': 'Agent start ho gaya' if started else 'Agent pehle se chal raha hai'
    })


@agent_bp.route('/stop', methods=['POST'])
def stop_agent():
    if not _is_admin():
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    agent.stop()
    return jsonify({'success': True, 'running': agent.running, 'message': 'Agent stop ho gaya'})


@agent_bp.route('/status', methods=['GET'])
def agent_status():
    if not _is_admin():
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    status = agent.get_status()
    status['success'] = True
    return jsonify(status)
