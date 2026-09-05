import cv2
import time
import json
import base64
import os
import requests
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, db

load_dotenv()

# -----------------------------
# AI CONFIG
# ---------------------------
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

# -----------------------------
# FIREBASE START
# -----------------------------

try:
    firebase_admin.get_app()
except ValueError:
    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred, {
        "databaseURL": FIREBASE_DATABASE_URL
    })

print("Firebase Connected")

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
            print("Organic Food Detected")
            print("Firebase /metal = 2")
        else:
            print(message)

        return result, message

    elif category == "plastic":
        ok, message = send_to_firebase(PLASTIC_VALUE)

        if ok:
            print("Plastic Item Detected")
            print("Firebase /metal = 3")
        else:
            print(message)

        return result, message

    else:
        print("Unknown Item")
        print("Firebase update nahi hui")
        return result, "Unknown - no Firebase update"

# -----------------------------
# DRAW SCREEN PANEL
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
            firebase_value = "2"
            display_category = "Organic Food"
        elif category == "plastic":
            firebase_value = "3"
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
            f"Firebase /metal: {firebase_value}",
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

# -----------------------------
# CAMERA START
# -----------------------------

cap = cv2.VideoCapture(1)

cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

if not cap.isOpened():
    print("Camera open nahi ho rahi")
    exit()

print("Agent Start")
print("Har 15 seconds item checking hogi")
print("Organic food / gala sra food par Firebase /metal = 2")
print("Plastic item par Firebase /metal = 3")
print("Exit ke liye Q press karo")

# -----------------------------
# MAIN LOOP
# -----------------------------

INSPECTION_INTERVAL = 15
last_check_time = 0
inspection_result = None
firebase_status = ""
future = None

executor = ThreadPoolExecutor(max_workers=1)

try:
    while True:
        ret, frame = cap.read()

        if not ret:
            print("Camera frame read nahi hua")
            break

        current_time = time.time()

        if current_time - last_check_time >= INSPECTION_INTERVAL and future is None:
            print("Item Checking Start...")
            frame_for_ai = frame.copy()
            future = executor.submit(check_and_send, frame_for_ai)
            last_check_time = current_time

        if future is not None and future.done():
            try:
                inspection_result, firebase_status = future.result()
            except Exception as e:
                print("Agent Error:", e)
                firebase_status = "Agent Error"
            finally:
                future = None

        analyzing = future is not None

        draw_result_panel(
            frame,
            inspection_result,
            firebase_status,
            analyzing
        )

        cv2.imshow("Smart Dustbin Agent", frame)

        key = cv2.waitKey(1) & 0xFF

        if key == ord("q") or key == ord("Q"):
            break

finally:
    cap.release()
    cv2.destroyAllWindows()
    executor.shutdown(wait=False)
    print("Agent Closed")