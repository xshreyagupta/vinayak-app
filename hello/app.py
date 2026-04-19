from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
import hashlib
import os

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Generates a random secret key for sessions

DATABASE = "crowdfund.db"

# =============================================================================
# DATABASE HELPERS
# =============================================================================

def get_db():
    """Open a connection to the SQLite database."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row   # Enables dict-style column access
    return conn


def init_db():
    """Create all tables if they do not already exist."""
    conn = get_db()
    cur  = conn.cursor()

    # --- Users ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT UNIQUE NOT NULL,
            email      TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --- Campaigns ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            title       TEXT    NOT NULL,
            description TEXT    NOT NULL,
            goal_amount REAL    NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # --- Donations ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS donations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            amount      REAL    NOT NULL,
            donated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
            FOREIGN KEY (user_id)     REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()


def hash_password(password: str) -> str:
    """Return SHA-256 hex digest of the given password string."""
    return hashlib.sha256(password.encode()).hexdigest()


# =============================================================================
# ROUTES
# =============================================================================

@app.route("/")
def index():
    """Homepage — fetch all campaigns with totals and render the template."""
    conn = get_db()
    campaigns = conn.execute("""
        SELECT
            c.id,
            c.title,
            c.description,
            c.goal_amount                  AS goal,
            c.created_at,
            u.username                     AS creator,
            COALESCE(SUM(d.amount), 0)     AS raised
        FROM campaigns c
        JOIN  users     u ON u.id = c.user_id
        LEFT JOIN donations d ON d.campaign_id = c.id
        GROUP BY c.id
        ORDER BY c.created_at DESC
    """).fetchall()
    conn.close()
    return render_template("index.html", campaigns=campaigns)


# ---------- AUTH ----------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Register a new user account."""
    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email    = request.form.get("email",    "").strip().lower()
        password = request.form.get("password", "")

        # Server-side validation
        if not username or not email or not password:
            flash("All fields are required.", "error")
            return render_template("index.html", page="signup")

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("index.html", page="signup")

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                (username, email, hash_password(password))
            )
            conn.commit()
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username or email already taken.", "error")
            return render_template("index.html", page="signup")
        finally:
            conn.close()

    return render_template("index.html", page="signup")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate an existing user."""
    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn  = get_db()
        user  = conn.execute(
            "SELECT * FROM users WHERE username = ? AND password = ?",
            (username, hash_password(password))
        ).fetchone()
        conn.close()

        if user:
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            flash(f"Welcome back, {user['username']}! 👋", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid username or password.", "error")

    return render_template("index.html", page="login")


@app.route("/logout")
def logout():
    """Clear the session and redirect to homepage."""
    session.clear()
    flash("You've been logged out.", "success")
    return redirect(url_for("index"))


# ---------- CAMPAIGNS ----------

@app.route("/campaigns/new", methods=["GET", "POST"])
def create_campaign():
    """Create a new campaign (login required)."""
    if not session.get("user_id"):
        flash("Please log in to create a campaign.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        title       = request.form.get("title",       "").strip()
        description = request.form.get("description", "").strip()
        goal_str    = request.form.get("goal_amount",  "0")

        # Validate goal
        try:
            goal = float(goal_str)
            if goal <= 0:
                raise ValueError
        except ValueError:
            flash("Goal amount must be a positive number.", "error")
            return render_template("index.html", page="create")

        if not title or not description:
            flash("Title and description are required.", "error")
            return render_template("index.html", page="create")

        conn = get_db()
        conn.execute(
            "INSERT INTO campaigns (user_id, title, description, goal_amount) VALUES (?, ?, ?, ?)",
            (session["user_id"], title, description, goal)
        )
        conn.commit()
        conn.close()

        flash("Campaign launched successfully! 🎉", "success")
        return redirect(url_for("index"))

    return render_template("index.html", page="create")


# ---------- DONATIONS ----------

@app.route("/campaigns/<int:campaign_id>/donate", methods=["POST"])
def donate(campaign_id):
    """Record a donation for a campaign (login required)."""
    if not session.get("user_id"):
        flash("Please log in to donate.", "error")
        return redirect(url_for("login"))

    # Validate amount
    try:
        amount = float(request.form.get("amount", "0"))
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash("Please enter a valid donation amount.", "error")
        return redirect(url_for("index"))

    conn = get_db()

    # Ensure campaign exists
    campaign = conn.execute(
        "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()

    if not campaign:
        conn.close()
        flash("Campaign not found.", "error")
        return redirect(url_for("index"))

    # Prevent over-donation on completed campaigns
    raised = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM donations WHERE campaign_id = ?",
        (campaign_id,)
    ).fetchone()["total"]

    if raised >= campaign["goal_amount"]:
        conn.close()
        flash("This campaign has already reached its goal!", "error")
        return redirect(url_for("index"))

    # Save donation
    conn.execute(
        "INSERT INTO donations (campaign_id, user_id, amount) VALUES (?, ?, ?)",
        (campaign_id, session["user_id"], amount)
    )
    conn.commit()
    conn.close()

    flash(f"Thank you! Your donation of ₹{amount:,.0f} was recorded. 🙌", "success")
    return redirect(url_for("index"))


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    init_db()
    print("=" * 55)
    print("  CrowdFund is running!")
    print("  Open your browser at:  http://127.0.0.1:5000")
    print("=" * 55)
    app.run(debug=True)
