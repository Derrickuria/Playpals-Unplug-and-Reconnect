from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import pandas as pd
from datetime import datetime, date, timezone 
import json, requests, base64
from flask_migrate import Migrate

app = Flask(__name__)
app.config['SECRET_KEY'] = 'playpals_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///playpals.db'

# Mail config

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'derekush02@gmail.com'
app.config['MAIL_PASSWORD'] = 'ureuycfammvkxgxl'
app.config['MAIL_DEFAULT_SENDER'] = ('PlayPals Admin', 'derekush02@gmail.com')

db = SQLAlchemy(app)
mail = Mail(app)
login_manager = LoginManager(app)
login_manager.login_view = "admin_login"
migrate = Migrate(app, db)
serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

# Daraja M-Pesa config

DARAJA_CONSUMER_KEY    = "JT1YZJ60FuUcG5l0BugTcGcueF3J5UDdnKLL2CyKaJMAIzvK"
DARAJA_CONSUMER_SECRET = "uavYW1kjAvzZsIm0j0ELVR3giQByG9QnFNAINRo6X1Ni1lPz4xd6ILRYFhDtgNyG"
DARAJA_SHORTCODE       = "174379"
DARAJA_PASSKEY         = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"
DARAJA_CALLBACK_URL    = "https://leporine-temperately-jennie.ngrok-free.dev"

# Models

class Admin(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    description = db.Column(db.Text)
    price = db.Column(db.Integer)
    available = db.Column(db.Boolean, default=True)
    image = db.Column(db.String(200))
    quantity = db.Column(db.Integer, default=1)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    location = db.Column(db.String(200))

    total_price = db.Column(db.Integer)
    deposit_paid = db.Column(db.Integer)
    balance_due = db.Column(db.Integer)

    status = db.Column(db.String(20), default="Pending")
    payment_status = db.Column(db.String(30), default="Awaiting Payment")

    rental_start = db.Column(db.DateTime)
    return_date = db.Column(db.DateTime)

    requested_start = db.Column(db.String(20))
    requested_end = db.Column(db.String(20))
    rental_days = db.Column(db.Integer, default=1)

    items = db.Column(db.Text)

    mpesa_checkout_id = db.Column(db.String(100))
    mpesa_receipt = db.Column(db.String(100))

# Helpers

def get_rented_counts():
    active_orders = Order.query.filter(Order.status.in_(["Pending", "Delivered"])).all()
    rented = {}
    for order in active_orders:
        items = json.loads(order.items) if order.items else []
        for item in items:
            name = item.get("name", "")
            rented[name] = rented.get(name, 0) + 1
    return rented

def sync_game_availability():
    rented_counts = get_rented_counts()
    games = Game.query.all()
    for game in games:
        rented = rented_counts.get(game.name, 0)
        available_stock = (game.quantity or 1) - rented
        game.available = available_stock > 0
    db.session.commit()

# Daraja helpers

def get_mpesa_token():
    url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    credentials = base64.b64encode(
        f"{DARAJA_CONSUMER_KEY}:{DARAJA_CONSUMER_SECRET}".encode()
    ).decode("utf-8")
    headers = {"Authorization": f"Basic {credentials}"}
    response = requests.get(url, headers=headers, timeout=30)
    return response.json().get("access_token")

def format_phone(phone):
    phone = phone.strip().replace(" ", "").replace("+", "")
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    return phone

def stk_push(phone, amount, order_id):
    token = get_mpesa_token()
    if not token:
        return None, "Failed to get M-Pesa token"

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(
        f"{DARAJA_SHORTCODE}{DARAJA_PASSKEY}{timestamp}".encode()
    ).decode("utf-8")

    payload = {
        "BusinessShortCode": DARAJA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": format_phone(phone),
        "PartyB": DARAJA_SHORTCODE,
        "PhoneNumber": format_phone(phone),
        "CallBackURL": DARAJA_CALLBACK_URL,
        "AccountReference": f"PlayPals-{order_id}",
        "TransactionDesc": f"PlayPals deposit for order {order_id}"
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    url = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
    response = requests.post(url, json=payload, headers=headers, timeout=30)
    try:
        result = response.json()
    except Exception:
         print("STK raw response:", response.status_code, response.text)
         return None, f"Safaricom returned empty response (status {response.status_code})"


    if result.get("ResponseCode") == "0":
        return result.get("CheckoutRequestID"), None
    else:
        return None, result.get("errorMessage") or result.get("ResponseDescription", "STK Push failed")

# Login

@login_manager.user_loader
def load_user(user_id):
    return Admin.query.get(int(user_id))

# Public routes

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/catalogue")
def catalogue():
    sync_game_availability()
    games = Game.query.filter_by(available=True).all()
    return render_template("catalogue.html", games=games)

@app.route("/cart")
def cart():
    return render_template("cart.html")

@app.route("/checkout")
def checkout():
    return render_template("checkout.html")

@app.route("/payment")
def payment():
    return render_template("payment.html")

@app.route("/orderConfirmation/<int:order_id>")
def order_confirmation(order_id):
    order = Order.query.get_or_404(order_id)
    items = json.loads(order.items) if order.items else []
    return render_template("orderConfirmation.html", order=order, items=items)

# Save order and trigger STK push

@app.route("/save_order", methods=["POST"])
def save_order():
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data received"}), 400

        items = data.get("items", [])
        total = int(data.get("total", 0))
        deposit = int(total * 0.5)
        balance = total - deposit
        rental_days = int(data.get("rental_days", 1))
        mpesa_phone = data.get("mpesa_phone", data.get("phone", ""))

        new_order = Order(
            customer_name=data.get("name"),
            phone=data.get("phone"),
            location=data.get("address"),
            total_price=total,
            deposit_paid=deposit,
            balance_due=balance,
            items=json.dumps(items),
            rental_days=rental_days,
            requested_start=data.get("rental_start"),
            requested_end=data.get("rental_end"),
            status="Pending",
            payment_status="Awaiting Payment"
        )

        db.session.add(new_order)
        db.session.commit()

        # STK push — non-blocking, order is saved regardless
        checkout_id = None
        stk_error = None
        try:
            checkout_id, stk_error = stk_push(mpesa_phone, deposit, new_order.id)
        except Exception as stk_ex:
            stk_error = str(stk_ex)
            print("STK Push error:", stk_ex)

        if checkout_id:
            new_order.mpesa_checkout_id = checkout_id
            db.session.commit()

        sync_game_availability()

        if checkout_id:
            return jsonify({
                "order_id": new_order.id,
                "checkout_id": checkout_id,
                "message": "STK Push sent! Check your phone to complete payment."
            })
        else:
            return jsonify({
                "order_id": new_order.id,
                "stk_error": stk_error or "STK Push failed. Please try again."
            })

    except Exception as e:
        print("Error saving order:", e)
        return jsonify({"error": "Failed to save order"}), 500

# Daraja callback

@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    try:
        data = request.json
        stk_callback = data["Body"]["stkCallback"]
        result_code = stk_callback["ResultCode"]
        checkout_id = stk_callback["CheckoutRequestID"]

        order = Order.query.filter_by(mpesa_checkout_id=checkout_id).first()
        if not order:
            return jsonify({"ResultCode": 0, "ResultDesc": "Order not found"})

        if result_code == 0:
            items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
            receipt = next((i["Value"] for i in items if i["Name"] == "MpesaReceiptNumber"), None)
            order.mpesa_receipt = receipt
            order.payment_status = "Deposit Paid"
        else:
            order.payment_status = "Payment Failed"

        db.session.commit()
    except Exception as e:
        print("Callback error:", e)

    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

# Check payment status

@app.route("/check_payment/<int:order_id>")
def check_payment(order_id):
    order = Order.query.get_or_404(order_id)
    return jsonify({
        "payment_status": order.payment_status,
        "mpesa_receipt": order.mpesa_receipt
    })

# Deliver and return orders

@app.route("/admin/deliver_order/<int:id>")
@login_required
def deliver_order(id):
    order = Order.query.get_or_404(id)
    order.status = "Delivered"
    order.rental_start = datetime.utcnow()
    db.session.commit()
    sync_game_availability()
    return generate_delivery_receipt(order)

@app.route("/admin/return_order/<int:id>")
@login_required
def return_order(id):
    order = Order.query.get_or_404(id)
    order.status = "Completed"
    order.return_date = datetime.utcnow()
    order.payment_status = "Fully Paid"
    order.balance_due = 0
    db.session.commit()
    sync_game_availability()
    return generate_final_receipt(order)

# PDF receipts

def generate_delivery_receipt(order):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, 800, "PLAYPALS - DELIVERY RECEIPT")
    c.setFont("Helvetica", 12)
    c.drawString(50, 765, f"Order ID     : #{order.id}")
    c.drawString(50, 745, f"Customer     : {order.customer_name}")
    c.drawString(50, 725, f"Phone        : {order.phone}")
    c.drawString(50, 705, f"Location     : {order.location}")
    c.drawString(50, 685, f"Rental Dates : {order.requested_start} to {order.requested_end} ({order.rental_days} days)")
    c.drawString(50, 665, f"Delivered On : {order.rental_start.strftime('%Y-%m-%d %H:%M') if order.rental_start else 'N/A'}")
    c.drawString(50, 635, f"Total Price  : Ksh {order.total_price}")
    c.drawString(50, 615, f"Deposit Paid : Ksh {order.deposit_paid}")
    c.drawString(50, 595, f"Balance Due  : Ksh {order.balance_due}  (payable on return)")
    if order.mpesa_receipt:
        c.drawString(50, 575, f"M-Pesa Ref   : {order.mpesa_receipt}")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, 545, "Items Rented:")
    c.setFont("Helvetica", 11)
    items = json.loads(order.items) if order.items else []
    y = 528
    for item in items:
        c.drawString(70, y, f"- {item.get('name', '')}  @ Ksh {item.get('price', '')} x {order.rental_days} day(s)")
        y -= 18
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y - 20, "Thank you for choosing PlayPals!")
    c.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"Delivery_Receipt_{order.id}.pdf", mimetype="application/pdf")

def generate_final_receipt(order):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, 800, "PLAYPALS - FINAL RECEIPT")
    c.setFont("Helvetica", 12)
    c.drawString(50, 765, f"Order ID     : #{order.id}")
    c.drawString(50, 745, f"Customer     : {order.customer_name}")
    c.drawString(50, 725, f"Phone        : {order.phone}")
    c.drawString(50, 705, f"Rental Dates : {order.requested_start} to {order.requested_end} ({order.rental_days} days)")
    c.drawString(50, 685, f"Delivered On : {order.rental_start.strftime('%Y-%m-%d %H:%M') if order.rental_start else 'N/A'}")
    c.drawString(50, 665, f"Returned On  : {order.return_date.strftime('%Y-%m-%d %H:%M') if order.return_date else 'N/A'}")
    c.drawString(50, 635, f"Total Paid   : Ksh {order.total_price}")
    c.drawString(50, 615, f"Status       : Completed")
    if order.mpesa_receipt:
        c.drawString(50, 595, f"M-Pesa Ref   : {order.mpesa_receipt}")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, 565, "Items Returned:")
    c.setFont("Helvetica", 11)
    items = json.loads(order.items) if order.items else []
    y = 548
    for item in items:
        c.drawString(70, y, f"- {item.get('name', '')}  @ Ksh {item.get('price', '')} x {order.rental_days} day(s)")
        y -= 18
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y - 20, "Thank you for choosing PlayPals! Come back soon.")
    c.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"Final_Receipt_{order.id}.pdf", mimetype="application/pdf")

# Export orders to Excel

@app.route("/admin/export_orders")
@login_required
def export_orders():
    orders = Order.query.all()
    data = []
    for o in orders:
        data.append({
            "Order ID": o.id,
            "Customer": o.customer_name,
            "Phone": o.phone,
            "Location": o.location,
            "Rental Start": o.requested_start,
            "Rental End": o.requested_end,
            "Days": o.rental_days,
            "Total": o.total_price,
            "Deposit": o.deposit_paid,
            "Balance": o.balance_due,
            "Status": o.status,
            "Payment Status": o.payment_status,
            "M-Pesa Receipt": o.mpesa_receipt
        })
    df = pd.DataFrame(data)
    buffer = BytesIO()
    df.to_excel(buffer, index=False)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="PlayPals_Orders.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# Admin auth

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        admin = Admin.query.filter_by(username=request.form["username"]).first()
        if admin and check_password_hash(admin.password, request.form["password"]):
            login_user(admin)
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials")
    return render_template("admin_login.html")

@app.route("/admin/register", methods=["GET", "POST"])
def admin_register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        email = request.form.get("email", "").strip()
        if Admin.query.filter_by(username=username).first():
            flash("Username already exists. Please choose another.")
        else:
            hashed_pw = generate_password_hash(password)
            new_admin = Admin(username=username, password=hashed_pw, email=email or None)
            db.session.add(new_admin)
            db.session.commit()
            flash("Admin account created successfully. Please log in.")
            return redirect(url_for("admin_login"))
    return render_template("admin_register.html")

@app.route("/admin/logout")
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for("home"))

# Forgot and reset password

@app.route("/admin/forgot_password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"].strip()
        admin = Admin.query.filter_by(email=email).first()

        if admin:
            token = serializer.dumps(email, salt="password-reset")
            reset_url = url_for("reset_password", token=token, _external=True)
            try:
                msg = Message(
                    subject="PlayPals — Password Reset Request",
                    recipients=[email]
                )
                msg.html = f"""
                <div style="font-family:Arial,sans-serif; max-width:500px; margin:auto; padding:30px;
                            background:#1a1a2e; color:#fff; border-radius:12px;">
                    <h2 style="color:#f5c542;">🎲 PlayPals Admin</h2>
                    <p>Hi <strong>{admin.username}</strong>,</p>
                    <p>We received a request to reset your password.
                       Click the button below to set a new one.</p>
                    <p>This link expires in <strong>30 minutes</strong>.</p>
                    <a href="{reset_url}"
                       style="display:inline-block; margin:20px 0; padding:12px 28px;
                              background:#f5c542; color:#1a1a2e; font-weight:bold;
                              border-radius:8px; text-decoration:none;">
                        Reset My Password
                    </a>
                    <p style="font-size:12px; opacity:0.6;">
                        If you didn't request this, ignore this email.
                        Your password won't change.
                    </p>
                </div>
                """
                mail.send(msg)
            except Exception as e:
                print("Mail error:", e)
                flash("Failed to send email. Please try again.")
                return render_template("forgotpassword.html")

        # Always show same message to avoid revealing registered emails
        flash("If that email is registered, a reset link has been sent.")
        return redirect(url_for("admin_login"))

    return render_template("forgotpassword.html")

@app.route("/admin/reset_password/<token>", methods=["GET", "POST"])
def reset_password(token):
    try:
        email = serializer.loads(token, salt="password-reset", max_age=1800)
    except SignatureExpired:
        flash("This reset link has expired. Please request a new one.")
        return redirect(url_for("forgotpassword"))
    except BadSignature:
        flash("Invalid reset link.")
        return redirect(url_for("forgotpassword"))

    admin = Admin.query.filter_by(email=email).first_or_404()

    if request.method == "POST":
        password = request.form["password"]
        confirm = request.form["confirm_password"]
        if password != confirm:
            flash("Passwords do not match.")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.")
        else:
            admin.password = generate_password_hash(password)
            db.session.commit()
            flash("Password reset successfully! Please log in.")
            return redirect(url_for("admin_login"))

    return render_template("resetpassword.html", token=token)

# Admin dashboard

@app.route("/admin/dashboard")
@login_required
def admin_dashboard():
    orders = Order.query.all()
    today = date.today()

    total_orders = len(orders)
    active_rentals = sum(1 for o in orders if o.status == "Delivered")
    completed = sum(1 for o in orders if o.status == "Completed")
    pending = sum(1 for o in orders if o.status == "Pending")
    total_revenue = sum((o.total_price or 0) for o in orders if o.status == "Completed")
    total_deposits = sum((o.deposit_paid or 0) for o in orders if o.status in ["Pending", "Delivered"])
    overdue_count = 0

    for order in orders:
        order.parsed_items = json.loads(order.items) if order.items else []
        order.overdue = False
        if order.status == "Delivered" and order.requested_end:
            try:
                end = datetime.strptime(order.requested_end, "%Y-%m-%d").date()
                order.overdue = today > end
                if order.overdue:
                    overdue_count += 1
            except:
                pass

    stats = {
        "total_orders": total_orders,
        "active_rentals": active_rentals,
        "completed": completed,
        "pending": pending,
        "total_revenue": total_revenue,
        "total_deposits": total_deposits,
        "overdue": overdue_count,
    }

    return render_template("admin_dashboard.html", stats=stats)

# Orders page

@app.route("/admin/orders")
@login_required
def admin_orders():
    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()
    today = date.today()

    query = Order.query
    if search:
        query = query.filter(
            db.or_(
                Order.customer_name.ilike(f"%{search}%"),
                Order.phone.ilike(f"%{search}%")
            )
        )
    if status_filter:
        query = query.filter_by(status=status_filter)

    orders = query.order_by(Order.id.desc()).all()

    for order in orders:
        order.parsed_items = json.loads(order.items) if order.items else []
        order.overdue = False
        if order.status == "Delivered" and order.requested_end:
            try:
                end = datetime.strptime(order.requested_end, "%Y-%m-%d").date()
                order.overdue = today > end
            except:
                pass

    return render_template("orders.html", orders=orders, search=search, status_filter=status_filter)

# INVENTORY PAGE

@app.route("/admin/inventory")
@login_required
def admin_inventory():
    games = Game.query.all()
    rented_counts = get_rented_counts()

    inventory = []
    for game in games:
        rented = rented_counts.get(game.name, 0)
        available_stock = (game.quantity or 1) - rented
        inventory.append({
            "game": game,
            "total": game.quantity,
            "rented": rented,
            "available": available_stock,
            "status": "Out of Stock" if available_stock <= 0 else ("Low Stock" if available_stock == 1 else "In Stock")
        })

    return render_template("inventory.html", inventory=inventory)

# GAME MANAGEMENT

@app.route("/admin/add_game", methods=["POST"])
@login_required
def add_game():
    try:
        name = request.form["name"]
        description = request.form["description"]
        price = int(request.form["price"])
        image = request.form["image"]
        quantity = int(request.form.get("quantity", 1))
        available = quantity > 0
        new_game = Game(name=name, description=description, price=price,
                        image=image, quantity=quantity, available=available)
        db.session.add(new_game)
        db.session.commit()
        flash(f"Game '{name}' added successfully!")
    except Exception as e:
        flash(f"Error adding game: {e}")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/edit_game/<int:id>", methods=["GET", "POST"])
@login_required
def edit_game(id):
    game = Game.query.get_or_404(id)
    if request.method == "POST":
        try:
            game.name = request.form["name"]
            game.description = request.form["description"]
            game.price = int(request.form["price"])
            game.image = request.form["image"]
            game.quantity = int(request.form.get("quantity", 1))
            db.session.commit()
            sync_game_availability()
            flash(f"Game '{game.name}' updated successfully!")
            return redirect(url_for("admin_inventory"))
        except Exception as e:
            flash(f"Error updating game: {e}")
    return render_template("edit_game.html", game=game)

@app.route("/admin/delete_game/<int:id>", methods=["POST"])
@login_required
def delete_game(id):
    game = Game.query.get_or_404(id)
    db.session.delete(game)
    db.session.commit()
    flash("Game deleted successfully")
    return redirect(url_for("admin_inventory"))

# RUN

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)