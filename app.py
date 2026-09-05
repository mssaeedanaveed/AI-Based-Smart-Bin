from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import firebase_admin
from firebase_admin import credentials, db
import yagmail
import random
import string
import os
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from datetime import datetime
import cv2
from ultralytics import YOLO
import google.generativeai as genai
from PIL import Image
import base64
import io

# Load variables from a local .env file (never commit .env to GitHub)
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Email Configuration
YAGMAIL_USER = os.environ.get('YAGMAIL_USER')
YAGMAIL_PASSWORD = os.environ.get('YAGMAIL_PASSWORD')

# Gemini API Configuration
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
genai.configure(api_key=GEMINI_API_KEY)

# Admin credentials
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')

# Firebase initialization
# Download your Firebase service account key from the Firebase console and
# save it locally (e.g. as 'firebase_key.json'). Never commit this file —
# it's excluded via .gitignore. Point FIREBASE_CREDENTIALS_PATH at it in .env.
try:
    cred = credentials.Certificate(os.environ.get('FIREBASE_CREDENTIALS_PATH'))
    firebase_admin.initialize_app(cred, {
        'databaseURL': os.environ.get('FIREBASE_DATABASE_URL')
    })
except Exception as e:
    print(f"Firebase initialization failed: {e}. Check your .env configuration.")

# ✅ Register the Dustbin AI Agent blueprint (webcam + Gemini classification
# agent). Registered AFTER Firebase init above so it reuses the same
# firebase_admin app/credentials instead of initializing a second one.
# Routes added: /admin/agent/start, /admin/agent/stop, /admin/agent/status
from a_blueprint import agent_bp

app.register_blueprint(agent_bp)

# Load YOLO models
try:
    plastic_model = YOLO(r'C:\Users\LAPTOP LAB\OneDrive - Iqra University Islamabad Chak Shahzad\Desktop\Project\best.pt')
    metal_model = YOLO('yolov8n.pt')  # Using YOLOv8n for metal detection
except Exception as e:
    print(f"Error loading YOLO models: {e}")
    plastic_model = None
    metal_model = None

# Gemini model for organic waste
try:
    gemini_model = genai.GenerativeModel('gemini-2.5-flash')
except:
    gemini_model = None


# Helper Functions
def send_email(to_email, subject, body):
    """Send email using yagmail"""
    try:
        yag = yagmail.SMTP(YAGMAIL_USER, YAGMAIL_PASSWORD)
        yag.send(to=to_email, subject=subject, contents=body)
        return True
    except Exception as e:
        print(f"Email sending failed: {e}")
        return False


def generate_verification_code():
    """Generate 6-digit verification code"""
    return ''.join(random.choices(string.digits, k=6))


def get_user_by_email(email):
    """Get user from Firebase by email"""
    try:
        users_ref = db.reference('users')
        users = users_ref.get()
        if users:
            for user_id, user_data in users.items():
                if user_data.get('email') == email:
                    return user_id, user_data
        return None, None
    except:
        return None, None


# Routes
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        # Validations
        if not name or not email or not password or not confirm_password:
            flash('All fields are required!', 'danger')
            return redirect(url_for('signup'))

        if len(name) < 3:
            flash('Name must be at least 3 characters long!', 'danger')
            return redirect(url_for('signup'))

        if '@' not in email or '.' not in email:
            flash('Invalid email format!', 'danger')
            return redirect(url_for('signup'))

        if len(password) < 6:
            flash('Password must be at least 6 characters long!', 'danger')
            return redirect(url_for('signup'))

        if password != confirm_password:
            flash('Passwords do not match!', 'danger')
            return redirect(url_for('signup'))

        # Check if email already exists
        user_id, user_data = get_user_by_email(email)
        if user_id:
            flash('Email already registered!', 'danger')
            return redirect(url_for('signup'))

        # Generate verification code
        verification_code = generate_verification_code()

        # Store temporary user data in session
        session['temp_user'] = {
            'name': name,
            'email': email,
            'password': password,
            'verification_code': verification_code
        }

        # Send verification email
        subject = "Smart Dust Bin - Email Verification"
        body = f"""
        Hello {name},

        Welcome to Smart Dust Bin Company!

        Your verification code is: {verification_code}

        Please enter this code to verify your email address.

        Best regards,
        Smart Dust Bin Team
        """

        if send_email(email, subject, body):
            flash('Verification code sent to your email!', 'success')
            return redirect(url_for('verify_email'))
        else:
            flash('Failed to send verification email. Please try again.', 'danger')
            return redirect(url_for('signup'))

    return render_template('signup.html')


@app.route('/verify_email', methods=['GET', 'POST'])
def verify_email():
    if 'temp_user' not in session:
        flash('Please sign up first!', 'danger')
        return redirect(url_for('signup'))

    if request.method == 'POST':
        entered_code = request.form.get('verification_code', '').strip()

        if entered_code == session['temp_user']['verification_code']:
            # Create user in Firebase
            temp_user = session['temp_user']
            users_ref = db.reference('users')
            new_user_ref = users_ref.push({
                'name': temp_user['name'],
                'email': temp_user['email'],
                'password': temp_user['password'],
                'approved': False,
                'created_at': datetime.now().isoformat(),
                'plastic_level': 0,
                'metal_level': 0,
                'organic_level': 0
            })

            session.pop('temp_user')
            flash('Account created successfully! Waiting for admin approval.', 'success')
            return redirect(url_for('signin'))
        else:
            flash('Invalid verification code!', 'danger')

    return render_template('verify_email.html')


@app.route('/signin', methods=['GET', 'POST'])
def signin():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()

        # Check if admin
        if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
            session['user_type'] = 'admin'
            session['email'] = email
            flash('Welcome Admin!', 'success')
            return redirect(url_for('admin_dashboard'))

        # Check regular user
        user_id, user_data = get_user_by_email(email)

        if not user_id:
            flash('Email not registered!', 'danger')
            return redirect(url_for('signin'))

        if user_data['password'] != password:
            flash('Incorrect password!', 'danger')
            return redirect(url_for('signin'))

        if not user_data.get('approved', False):
            flash('Your account is pending admin approval!', 'warning')
            return redirect(url_for('signin'))

        # Login successful
        session['user_type'] = 'user'
        session['user_id'] = user_id
        session['email'] = email
        session['name'] = user_data['name']
        flash(f'Welcome {user_data["name"]}!', 'success')
        return redirect(url_for('user_dashboard'))

    return render_template('signin.html')


@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()

        # Check if email exists
        user_id, user_data = get_user_by_email(email)

        if not user_id:
            flash('Email not found in database!', 'danger')
            return redirect(url_for('forgot_password'))

        # Generate verification code
        verification_code = generate_verification_code()

        # Store in session
        session['reset_data'] = {
            'email': email,
            'user_id': user_id,
            'verification_code': verification_code
        }

        # Send email
        subject = "Smart Dust Bin - Password Reset"
        body = f"""
        Hello,

        You requested to reset your password.

        Your verification code is: {verification_code}

        If you didn't request this, please ignore this email.

        Best regards,
        Smart Dust Bin Team
        """

        if send_email(email, subject, body):
            flash('Verification code sent to your email!', 'success')
            return redirect(url_for('reset_password_verify'))
        else:
            flash('Failed to send email. Please try again.', 'danger')

    return render_template('forgot_password.html')


@app.route('/reset_password_verify', methods=['GET', 'POST'])
def reset_password_verify():
    if 'reset_data' not in session:
        flash('Please enter your email first!', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        entered_code = request.form.get('verification_code', '').strip()

        if entered_code == session['reset_data']['verification_code']:
            return redirect(url_for('reset_password'))
        else:
            flash('Invalid verification code!', 'danger')

    return render_template('reset_password_verify.html')


@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    if 'reset_data' not in session:
        flash('Session expired. Please try again!', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if len(new_password) < 6:
            flash('Password must be at least 6 characters long!', 'danger')
            return redirect(url_for('reset_password'))

        if new_password != confirm_password:
            flash('Passwords do not match!', 'danger')
            return redirect(url_for('reset_password'))

        # Update password in Firebase
        user_id = session['reset_data']['user_id']
        user_ref = db.reference(f'users/{user_id}')
        user_ref.update({'password': new_password})

        session.pop('reset_data')
        flash('Password reset successfully! Please login.', 'success')
        return redirect(url_for('signin'))

    return render_template('reset_password.html')


@app.route('/admin/dashboard')
def admin_dashboard():
    if session.get('user_type') != 'admin':
        flash('Access denied!', 'danger')
        return redirect(url_for('signin'))

    # Get all users
    users_ref = db.reference('users')
    users_data = users_ref.get()

    users = []
    if users_data:
        for user_id, user_info in users_data.items():
            users.append({
                'id': user_id,
                **user_info
            })

    return render_template('admin_dashboard.html', users=users)


@app.route('/admin/approve/<user_id>')
def approve_user(user_id):
    if session.get('user_type') != 'admin':
        flash('Access denied!', 'danger')
        return redirect(url_for('signin'))

    # Update user status
    user_ref = db.reference(f'users/{user_id}')
    user_data = user_ref.get()

    if user_data:
        user_ref.update({'approved': True})

        # Send approval email
        subject = "Smart Dust Bin - Account Approved"
        body = f"""
        Hello {user_data['name']},

        Congratulations! Your account has been approved by the admin.

        You can now login to your account and start using Smart Dust Bin services.

        Best regards,
        Smart Dust Bin Team
        """
        send_email(user_data['email'], subject, body)

        flash('User approved successfully!', 'success')

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/reject/<user_id>')
def reject_user(user_id):
    if session.get('user_type') != 'admin':
        flash('Access denied!', 'danger')
        return redirect(url_for('signin'))

    # Get user data before deletion
    user_ref = db.reference(f'users/{user_id}')
    user_data = user_ref.get()

    if user_data:
        # Send rejection email
        subject = "Smart Dust Bin - Account Rejected"
        body = f"""
        Hello {user_data['name']},

        We regret to inform you that your account application has been rejected.

        If you have any questions, please contact our support team.

        Best regards,
        Smart Dust Bin Team
        """
        send_email(user_data['email'], subject, body)

        # Delete user
        user_ref.delete()
        flash('User rejected and removed!', 'success')

    return redirect(url_for('admin_dashboard'))


@app.route('/user/dashboard')
def user_dashboard():
    if session.get('user_type') != 'user':
        flash('Please login first!', 'danger')
        return redirect(url_for('signin'))

    # Get user data
    user_ref = db.reference(f'users/{session["user_id"]}')
    user_data = user_ref.get()

    return render_template('user_dashboard.html', user=user_data)


import os
from datetime import datetime
from flask import request, jsonify, session, flash, redirect, url_for, render_template
from werkzeug.utils import secure_filename

CONF_THRESHOLD = 0.60  # ✅ 60%


@app.route('/user/plastic_collector', methods=['GET', 'POST'])
def plastic_collector():
    if session.get('user_type') != 'user':
        flash('Please login first!', 'danger')
        return redirect(url_for('signin'))

    if request.method == 'POST':
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'No image uploaded'}), 400

        file = request.files['image']
        if not file or file.filename == '':
            return jsonify({'success': False, 'error': 'No image selected'}), 400

        if not plastic_model:
            return jsonify({'success': False, 'error': 'Model not loaded'}), 500

        filename = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
        upload_dir = app.config.get('UPLOAD_FOLDER', 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        filepath = os.path.join(upload_dir, filename)
        file.save(filepath)

        try:
            # YOLO inference
            results = plastic_model(filepath)

            detections = []
            plastic_count = 0

            for result in results:
                boxes = getattr(result, "boxes", None)
                if boxes is None:
                    continue

                for box in boxes:
                    confidence = float(box.conf[0])

                    # ✅ FILTER: only >= 60% confidence
                    if confidence < CONF_THRESHOLD:
                        continue

                    class_id = int(box.cls[0])
                    class_name = result.names.get(class_id, str(class_id)) if hasattr(result, "names") else str(
                        class_id)

                    detections.append({
                        'class': class_name,
                        'confidence': confidence
                    })
                    plastic_count += 1

            # ✅ If NO strong plastic detected => no increase, no DB update
            if plastic_count == 0:
                message = f"Plastic detect nahi hua (confidence >= {int(CONF_THRESHOLD * 100)}% required)."
                level_increase = 0

                # (Optional) log detection attempt
                detections_ref = db.reference(f'detections/{session["user_id"]}/plastic')
                detections_ref.push({
                    'timestamp': datetime.now().isoformat(),
                    'image_path': filename,
                    'detections': [],
                    'count': 0,
                    'level_increase': 0,
                    'message': message
                })

                # keep same level
                user_ref = db.reference(f'users/{session["user_id"]}')
                current_data = user_ref.get() or {}
                current_level = int(current_data.get('plastic_level', 0) or 0)

                return jsonify({
                    'success': True,
                    'message': message,
                    'detections': [],
                    'count': 0,
                    'level_increase': 0,
                    'new_level': current_level
                })

            # ✅ Calculate level increase ONLY from strong detections
            # 10% per item, max 100
            level_increase = min(plastic_count * 10, 100)

            # Update user's plastic level
            user_ref = db.reference(f'users/{session["user_id"]}')
            current_data = user_ref.get() or {}
            current_level = int(current_data.get('plastic_level', 0) or 0)
            new_plastic_level = min(current_level + level_increase, 100)

            # Save detection data
            detections_ref = db.reference(f'detections/{session["user_id"]}/plastic')
            detections_ref.push({
                'timestamp': datetime.now().isoformat(),
                'image_path': filename,
                'detections': detections,
                'count': plastic_count,
                'level_increase': level_increase,
                'message': 'Plastic detect hua. Bin level increase kar diya gaya.'
            })

            user_ref.update({'plastic_level': new_plastic_level})

            return jsonify({
                'success': True,
                'message': 'Plastic detect hua.',
                'detections': detections,
                'count': plastic_count,
                'level_increase': level_increase,
                'new_level': new_plastic_level
            })

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    return render_template('plastic_collector.html')


@app.route('/user/metal_collector', methods=['GET', 'POST'])
def metal_collector():
    if session.get('user_type') != 'user':
        flash('Please login first!', 'danger')
        return redirect(url_for('signin'))

    if request.method == 'POST':
        if 'image' not in request.files:
            return jsonify({'error': 'No image uploaded'}), 400

        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No image selected'}), 400

        if file:
            filename = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            # Detect metal items (bottle, cup, cell phone, etc.)
            if metal_model:
                results = metal_model(filepath)
                detections = []
                metal_count = 0

                # Metal-related classes in COCO dataset
                metal_classes = ['bottle', 'cup', 'cell phone', 'fork', 'knife', 'spoon', 'scissors']

                for result in results:
                    boxes = result.boxes
                    for box in boxes:
                        class_id = int(box.cls[0])
                        class_name = result.names[class_id]
                        confidence = float(box.conf[0])

                        if class_name in metal_classes:
                            detections.append({
                                'class': class_name,
                                'confidence': confidence
                            })
                            metal_count += 1

                # Calculate level increase
                level_increase = min(metal_count * 10, 100)

                # Update user's metal level
                user_ref = db.reference(f'users/{session["user_id"]}')
                current_data = user_ref.get()
                new_metal_level = min(current_data.get('metal_level', 0) + level_increase, 100)

                # Save detection data
                detections_ref = db.reference(f'detections/{session["user_id"]}/metal')
                detections_ref.push({
                    'timestamp': datetime.now().isoformat(),
                    'image_path': filename,
                    'detections': detections,
                    'count': metal_count,
                    'level_increase': level_increase
                })

                user_ref.update({'metal_level': new_metal_level})

                return jsonify({
                    'success': True,
                    'detections': detections,
                    'count': metal_count,
                    'level_increase': level_increase,
                    'new_level': new_metal_level
                })
            else:
                return jsonify({'error': 'Model not loaded'}), 500

    return render_template('metal_collector.html')


import os, json
from datetime import datetime
from flask import request, jsonify, session, flash, redirect, url_for, render_template
from werkzeug.utils import secure_filename
from PIL import Image

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def clamp(n, a, b):
    return max(a, min(b, n))


@app.route('/user/organic_collector', methods=['GET', 'POST'])
def organic_collector():
    if session.get('user_type') != 'user':
        flash('Please login first!', 'danger')
        return redirect(url_for('signin'))

    if request.method == 'POST':
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'No image uploaded'}), 400

        file = request.files['image']
        if not file or file.filename == '':
            return jsonify({'success': False, 'error': 'No image selected'}), 400

        if not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Invalid file type. Use png/jpg/jpeg/webp.'}), 400

        # Save file
        filename = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
        upload_dir = app.config.get('UPLOAD_FOLDER', 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        filepath = os.path.join(upload_dir, filename)
        file.save(filepath)

        if not gemini_model:
            return jsonify({'success': False, 'error': 'Gemini model not loaded'}), 500

        try:
            # Open image safely
            img = Image.open(filepath)
            img.verify()
            img = Image.open(filepath)

            # ✅ Schema WITHOUT minimum/maximum
            response_schema = {
                "type": "OBJECT",
                "properties": {
                    "is_organic": {"type": "BOOLEAN"},
                    "confidence": {"type": "NUMBER"},
                    "organic_items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "name": {"type": "STRING"},
                                "confidence": {"type": "NUMBER"}
                            },
                            "required": ["name", "confidence"]
                        }
                    },
                    "non_organic_items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "name": {"type": "STRING"},
                                "confidence": {"type": "NUMBER"}
                            },
                            "required": ["name", "confidence"]
                        }
                    },
                    "quantity": {"type": "STRING"},
                    "level_percentage": {"type": "INTEGER"},
                    "reason": {"type": "STRING"}
                },
                "required": [
                    "is_organic", "confidence",
                    "organic_items", "non_organic_items",
                    "quantity", "level_percentage", "reason"
                ]
            }

            prompt = """
You are a STRICT organic/compost waste classifier.

Organic examples: food waste, peels, leftovers, rotten food, leaves, grass, plant material, tea bags, coffee grounds.
Non-organic examples: plastic, metal, glass, paper packaging, wrappers, bottles, electronics, fabric.

Return STRICT JSON only (no extra text) with:
is_organic, confidence (0..1),
organic_items [{name, confidence}],
non_organic_items [{name, confidence}],
quantity ("low"|"medium"|"high"),
level_percentage (0..100),
reason.

Rule:
Set is_organic=true ONLY if organic_items has at least 1 clear organic item AND confidence >= 0.70.
If strong non-organic is present, prefer is_organic=false.
"""

            response = gemini_model.generate_content(
                [prompt, img],
                generation_config={
                    "response_mime_type": "application/json",
                    "response_schema": response_schema,
                    "temperature": 0.2
                }
            )

            raw = (response.text or "").strip()

            # Parse JSON
            try:
                data = json.loads(raw)
            except Exception:
                start = raw.find("{")
                end = raw.rfind("}")
                if start != -1 and end != -1 and end > start:
                    data = json.loads(raw[start:end + 1])
                else:
                    return jsonify({'success': False, 'error': 'Model did not return JSON', 'raw': raw}), 500

            # helpers
            def clean_items(items):
                cleaned = []
                for it in (items or []):
                    if not isinstance(it, dict):
                        continue
                    name = (it.get("name") or "").strip()
                    conf = clamp(float(it.get("confidence", 0) or 0), 0.0, 1.0)
                    if name:
                        cleaned.append({"name": name, "confidence": conf})
                return cleaned

            model_conf = clamp(float(data.get("confidence", 0) or 0), 0.0, 1.0)
            organic_items = clean_items(data.get("organic_items"))
            non_organic_items = clean_items(data.get("non_organic_items"))

            quantity = (data.get("quantity") or "low").lower()
            if quantity not in ("low", "medium", "high"):
                quantity = "low"

            level_percentage = clamp(int(data.get("level_percentage", 0) or 0), 0, 100)

            # ---- STRICT decision ----
            has_strong_organic_item = any(i["confidence"] >= 0.60 for i in organic_items)
            strong_non_organic = any(i["confidence"] >= 0.70 for i in non_organic_items)

            is_organic_strict = (model_conf >= 0.70) and has_strong_organic_item and (not strong_non_organic)

            # ✅ HARD ENFORCE: If NOT organic => set bin data to ZERO
            if not is_organic_strict:
                result = {
                    "is_organic": False,
                    "confidence": model_conf,
                    "organic_items": [],  # must be empty
                    "non_organic_items": non_organic_items,
                    "quantity": "low",
                    "level_percentage": 0,  # must be 0
                    "reason": "Organic waste detect nahi hua."
                }
                level_increase = 0
                message = "Organic waste detect nahi hua. Bin level 0% aur increase 0."
            else:
                result = {
                    "is_organic": True,
                    "confidence": model_conf,
                    "organic_items": organic_items,
                    "non_organic_items": non_organic_items,
                    "quantity": quantity,
                    "level_percentage": level_percentage,
                    "reason": (data.get("reason") or "").strip() or "Organic waste detect hua."
                }

                if quantity == "high":
                    level_increase = 40
                elif quantity == "medium":
                    level_increase = 25
                else:
                    level_increase = 10

                message = "Organic waste detect hua. Bin me add kar diya gaya."

            # ---- DB logging (always log), but update organic_level ONLY if organic ----
            user_ref = db.reference(f'users/{session["user_id"]}')
            current_data = user_ref.get() or {}
            current_level = int(current_data.get('organic_level', 0) or 0)

            new_organic_level = clamp(current_level + level_increase, 0, 100)

            detections_ref = db.reference(f'detections/{session["user_id"]}/organic')
            detections_ref.push({
                'timestamp': datetime.now().isoformat(),
                'image_path': filename,
                'analysis_json': result,
                'raw_model_text': raw,
                'level_increase': level_increase,
                'message': message
            })

            # ✅ IMPORTANT: only update if organic TRUE
            if result["is_organic"]:
                user_ref.update({'organic_level': new_organic_level})
            else:
                # keep same
                new_organic_level = current_level

            return jsonify({
                'success': True,
                'filename': filename,
                'message': message,
                'result': result,
                'level_increase': level_increase,
                'new_level': new_organic_level
            })

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    return render_template('organic_collector.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully!', 'success')
    return redirect(url_for('index'))


from datetime import datetime

ADMIN_NOTIFY_EMAIL = "mssaeedanaveed@gmail.com"

FULL_THRESHOLD = 100  # ✅ sirf 100% wale show + alert

BIN_FIELDS = {
    "plastic": "plastic_level",
    "metal": "metal_level",
    "organic": "organic_level",
}


def _now():
    return datetime.now().isoformat()


def push_admin_notification(title, message, extra=None):
    ref = db.reference("notifications/admin")
    payload = {"title": title, "message": message, "timestamp": _now(), "read": False}
    if extra:
        payload["extra"] = extra
    ref.push(payload)


def push_user_notification(user_id, title, message, extra=None):
    ref = db.reference(f"notifications/users/{user_id}")
    payload = {"title": title, "message": message, "timestamp": _now(), "read": False}
    if extra:
        payload["extra"] = extra
    ref.push(payload)


def send_full_bin_emails(user_email, user_name, bin_type, level):
    # user email
    subject_user = f"Smart Dust Bin Alert: {bin_type.capitalize()} bin FULL"
    body_user = f"""
Hello {user_name},

Your {bin_type} bin is FULL.

Current level: {level}% (FULL)

Please wait—admin/collector will clear it soon.

Regards,
Smart Dust Bin Team
"""
    send_email(user_email, subject_user, body_user)

    # admin email
    subject_admin = f"[ALERT] {bin_type.capitalize()} bin FULL - {user_name}"
    body_admin = f"""
Admin Alert,

User: {user_name}
Email: {user_email}
Bin: {bin_type}
Level: {level}% (FULL)
Time: {_now()}

Please clear from Admin Notify page.

Regards,
System
"""
    send_email(ADMIN_NOTIFY_EMAIL, subject_admin, body_admin)


def send_cleared_emails(user_email, user_name, bin_type, cleared_by, apartment, note=""):
    # user email
    subject_user = f"Smart Dust Bin: {bin_type.capitalize()} bin cleared"
    body_user = f"""
Hello {user_name},

Your {bin_type} bin has been cleared.

Cleared by: {cleared_by}
Apartment/Location: {apartment}
Time: {_now()}
Note: {note}

Regards,
Smart Dust Bin Team
"""
    send_email(user_email, subject_user, body_user)

    # admin email
    subject_admin = f"[CLEARED] {bin_type.capitalize()} bin cleared - {user_name}"
    body_admin = f"""
Admin Update,

User: {user_name}
Email: {user_email}
Bin: {bin_type}
Cleared by: {cleared_by}
Apartment/Location: {apartment}
Time: {_now()}
Note: {note}

Regards,
System
"""
    send_email(ADMIN_NOTIFY_EMAIL, subject_admin, body_admin)


def scan_and_create_full_alerts():
    """
    ✅ Auto scan: find users with bin level >= 100
    ✅ Create alert only if not already open
    ✅ Send emails/notifications only once (no spam)
    """
    users = db.reference("users").get() or {}
    created = 0

    for user_id, u in users.items():
        if not u.get("approved", False):
            continue

        user_email = (u.get("email") or "").strip()
        user_name = (u.get("name") or "User").strip()

        for bin_type, field in BIN_FIELDS.items():
            level = int(u.get(field, 0) or 0)

            if level >= FULL_THRESHOLD:
                alert_ref = db.reference(f"alerts/{user_id}/{bin_type}")
                existing = alert_ref.get()

                # ✅ if already open => don't resend
                if isinstance(existing, dict) and existing.get("status") == "open":
                    continue

                alert_data = {
                    "status": "open",
                    "user_id": user_id,
                    "user_email": user_email,
                    "user_name": user_name,
                    "bin_type": bin_type,
                    "level": level,
                    "threshold": FULL_THRESHOLD,
                    "created_at": _now(),
                    "last_sent_at": _now(),
                }
                alert_ref.set(alert_data)
                created += 1

                # Notifications
                push_admin_notification(
                    "Bin FULL Alert",
                    f"{user_name} ({user_email}) - {bin_type} bin FULL ({level}%)",
                    extra={"user_id": user_id, "bin_type": bin_type, "level": level},
                )
                push_user_notification(
                    user_id,
                    "Bin FULL",
                    f"Your {bin_type} bin is FULL ({level}%). Admin will clear it soon.",
                    extra={"bin_type": bin_type, "level": level},
                )

                # Emails
                if user_email:
                    send_full_bin_emails(user_email, user_name, bin_type, level)

    return created


@app.route("/admin/notify", methods=["GET", "POST"])
def admin_notify():
    if session.get("user_type") != "admin":
        flash("Access denied!", "danger")
        return redirect(url_for("signin"))

    # -------- POST actions --------
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        # Manual scan button (still works)
        if action == "scan_now":
            created = scan_and_create_full_alerts()
            flash(f"Scan done. New FULL alerts: {created}", "success")
            return redirect(url_for("admin_notify"))

        # Clear bin
        if action == "clear_bin":
            user_id = (request.form.get("user_id") or "").strip()
            bin_type = (request.form.get("bin_type") or "").strip()
            cleared_by = (request.form.get("cleared_by") or "Admin").strip()
            apartment = (request.form.get("apartment") or "N/A").strip()
            note = (request.form.get("note") or "").strip()

            if not user_id or bin_type not in BIN_FIELDS:
                flash("Invalid request!", "danger")
                return redirect(url_for("admin_notify"))

            # set selected bin level to 0
            field = BIN_FIELDS[bin_type]
            user_ref = db.reference(f"users/{user_id}")
            user_data = user_ref.get() or {}
            user_email = (user_data.get("email") or "").strip()
            user_name = (user_data.get("name") or "User").strip()

            user_ref.update({field: 0})

            # resolve alert
            alert_ref = db.reference(f"alerts/{user_id}/{bin_type}")
            existing = alert_ref.get() or {}
            existing.update({
                "status": "resolved",
                "resolved_at": _now(),
                "resolved_info": {
                    "cleared_by": cleared_by,
                    "apartment": apartment,
                    "note": note
                }
            })
            alert_ref.set(existing)

            # save cleanup history
            db.reference("cleanups").push({
                "timestamp": _now(),
                "user_id": user_id,
                "user_email": user_email,
                "user_name": user_name,
                "bin_type": bin_type,
                "cleared_by": cleared_by,
                "apartment": apartment,
                "note": note
            })

            # notifications
            push_admin_notification(
                "Bin Cleared",
                f"{user_name} - {bin_type} cleared by {cleared_by} (Apartment: {apartment})",
                extra={"user_id": user_id, "bin_type": bin_type, "cleared_by": cleared_by, "apartment": apartment},
            )
            push_user_notification(
                user_id,
                "Bin Cleared",
                f"Your {bin_type} bin has been cleared by {cleared_by}.",
                extra={"bin_type": bin_type, "cleared_by": cleared_by, "apartment": apartment},
            )

            # emails
            if user_email:
                send_cleared_emails(user_email, user_name, bin_type, cleared_by, apartment, note)

            # confirm to admin email too
            send_email(
                ADMIN_NOTIFY_EMAIL,
                f"[CONFIRM] Cleared {bin_type} bin - {user_name}",
                f"Cleared {bin_type} bin for {user_name} ({user_email}). Apartment: {apartment}. By: {cleared_by}. Note: {note}. Time: {_now()}",
            )

            flash(f"{bin_type.capitalize()} cleared for {user_name}. Level set to 0.", "success")
            return redirect(url_for("admin_notify"))

        flash("Unknown action!", "danger")
        return redirect(url_for("admin_notify"))

    # -------- GET: AUTO scan on page open --------
    created = scan_and_create_full_alerts()
    if created > 0:
        flash(f"Auto-scan: New FULL alerts created: {created}", "info")

    # Load open alerts + cleanup history
    users = db.reference("users").get() or {}
    alerts_root = db.reference("alerts").get() or {}
    cleanups_root = db.reference("cleanups").get() or {}

    open_alerts = []
    for user_id, bins in (alerts_root or {}).items():
        for bin_type, alert in (bins or {}).items():
            if isinstance(alert, dict) and alert.get("status") == "open":
                # only show FULL threshold (100)
                level = int(alert.get("level", 0) or 0)
                if level < FULL_THRESHOLD:
                    continue

                u = users.get(user_id, {}) or {}
                open_alerts.append({
                    "user_id": user_id,
                    "user_name": alert.get("user_name") or u.get("name") or "User",
                    "user_email": alert.get("user_email") or u.get("email") or "",
                    "bin_type": bin_type,
                    "level": level,
                    "created_at": alert.get("created_at", ""),
                })

    # cleanup history latest first
    cleanups = []
    for cid, c in (cleanups_root or {}).items():
        if isinstance(c, dict):
            cleanups.append({"id": cid, **c})
    cleanups.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    # sort alerts by level desc
    open_alerts.sort(key=lambda x: (-int(x["level"] or 0), x["bin_type"]))

    return render_template(
        "admin_notify.html",
        open_alerts=open_alerts,
        cleanups=cleanups,
        threshold=FULL_THRESHOLD,
        admin_email=ADMIN_NOTIFY_EMAIL
    )




DUSTBIN_TYPES = ['Metal', 'Organic', 'Plastic']
DUSTBIN_ALERT_EMAIL = "mssaeedanaveed@gmail.com"


def get_dustbin_data():
    """Firebase RTDB se teeno bins ka live data fetch karta hai."""
    bins_data = {}
    for bin_type in DUSTBIN_TYPES:
        try:
            node = db.reference(bin_type).get() or {}
        except Exception as e:
            print(f"Firebase read failed for {bin_type}: {e}")
            node = {}

        bins_data[bin_type] = {
            'dustbinStatus': node.get('dustbinStatus', 'EMPTY'),
            'dustbinLevel': node.get('dustbinLevel', 0),
            'dustbinHeight': node.get('dustbinHeight', 0),
        }
    return bins_data


# ✅ FIX for "jinja2.exceptions.UndefinedError: 'bins' is undefined"
# admin_base.html (jise saare admin pages extend karte hain) shayad
# navbar/sidebar mein 'bins' use karta hai. Ye context processor 'bins'
# ko HAR template render ke liye automatically available kara deta hai,
# taake kisi bhi admin route (dashboard, notify, etc.) mein error na aaye
# — chahe wo route khud 'bins' pass kare ya na kare.
_DEFAULT_BINS = {b: {'dustbinStatus': 'EMPTY', 'dustbinLevel': 0, 'dustbinHeight': 0} for b in DUSTBIN_TYPES}


@app.context_processor
def inject_dustbin_bins():
    if session.get('user_type') != 'admin':
        return {'bins': _DEFAULT_BINS}
    try:
        return {'bins': get_dustbin_data()}
    except Exception as e:
        print(f"inject_dustbin_bins failed: {e}")
        return {'bins': _DEFAULT_BINS}


def check_and_send_full_alerts(bins_data):
    """
    Jis bin ka status 'FULL' ho, uske liye email bhejta hai — sirf ek
    baar, jab tak wo bin dobara EMPTY/na-FULL na ho jaye (dedupe via
    Firebase 'bin_alerts/<BinType>' node).
    """
    for bin_type in DUSTBIN_TYPES:
        info = bins_data.get(bin_type, {})
        status = str(info.get('dustbinStatus', '')).strip().upper()
        level = info.get('dustbinLevel', 0)

        alert_ref = db.reference(f'bin_alerts/{bin_type}')
        try:
            alert_state = alert_ref.get() or {}
        except Exception:
            alert_state = {}

        already_sent = bool(alert_state.get('sent', False))

        if status == 'FULL':
            if not already_sent:
                subject = f"Smart Dust Bin Alert: {bin_type} Bin is FULL"
                body = f"""
Hello,

The {bin_type} dustbin is now FULL.

Status: {status}
Level: {level}%
Time: {_now()}

Please arrange for it to be emptied soon.

Regards,
Smart Dust Bin System
"""
                if send_email(DUSTBIN_ALERT_EMAIL, subject, body):
                    alert_ref.set({
                        'sent': True,
                        'sent_at': _now(),
                        'level': level
                    })
        else:
            # Bin FULL nahi hai (ya khali ho chuka hai) -> reset flag
            # taake agli dafa FULL hone par email dobara chali jaye.
            if already_sent:
                alert_ref.set({'sent': False})


@app.route('/admin/dustbin_status')
def dustbin_status():
    """Admin ke liye teeno bins ki realtime status page."""
    if session.get('user_type') != 'admin':
        flash('Access denied!', 'danger')
        return redirect(url_for('signin'))

    bins_data = get_dustbin_data()
    check_and_send_full_alerts(bins_data)

    return render_template('dustbin_status.html', bins=bins_data)


@app.route('/api/dustbin_status')
def api_dustbin_status():
    """
    JSON API — page ke JS ise har kuch second baad poll karke
    progress bars/status ko bina reload kiye update karta hai.
    """
    if session.get('user_type') not in ('admin', 'user'):
        return jsonify({'error': 'Access denied'}), 403

    bins_data = get_dustbin_data()
    check_and_send_full_alerts(bins_data)

    return jsonify({'success': True, 'bins': bins_data, 'timestamp': _now()})


if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5000)