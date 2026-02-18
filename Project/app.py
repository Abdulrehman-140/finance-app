from flask import Flask, render_template, request, redirect, jsonify,url_for,g
import sqlite3
from datetime import datetime, timedelta
import pytz
import database  
from database import init_db
import traceback

app = Flask(__name__)
database.init_db()

from math import isfinite

def safe_float(x):
    try:
        return float(x or 0)
    except:
        return 0.0

def generate_ai_insights(conn, total_budget, total_spent, remaining):
    """
    Returns a dict with:
      - avg_daily_spend (last 14 days)
      - days_until_zero
      - fastest_growing (name, ratio, last7, prev7)
      - health_scores: list of {name, assigned, spent, remaining_pct, score}
      - suggestions: short advice strings
      - category_bar_data: lists for chart (labels, assigned[], spent[])
    """
    cur = conn.cursor()

    # --- avg daily spend (last 14 days) using expenses table (fall back to subcategory amounts)
    # Sum over last 14 days
    cur.execute("SELECT COALESCE(SUM(amount),0) as total14 FROM expenses WHERE DATE(date) >= DATE('now','-13 days')")
    total14 = safe_float(cur.fetchone()["total14"])
    avg_daily_spend = total14 / 14.0

    # --- days until budget runs out (prediction)
    days_until_zero = None
    if avg_daily_spend > 0:
        days_until_zero = remaining / avg_daily_spend
        if days_until_zero < 0:
            days_until_zero = 0
        else:
            # clamp reasonable large numbers
            if days_until_zero > 36500:
                days_until_zero = None

    # --- fastest growing category: compare last 7 vs previous 7 days
    # We'll compute for each category:
    cur.execute("SELECT id, name, COALESCE(assigned,0) as assigned FROM categories")
    categories = cur.fetchall()
    category_trends = []
    category_bar_labels = []
    category_assigned = []
    category_spent = []
    for c in categories:
        cid = c["id"]
        cname = c["name"]
        assigned = safe_float(c["assigned"])

        # last 7 days spend (from expenses table via subcategory link)
        cur.execute("""
            SELECT COALESCE(SUM(e.amount),0) as s7
            FROM expenses e
            JOIN subcategories s ON s.id = e.subcategory_id
            WHERE s.category_id = ? AND DATE(e.date) >= DATE('now','-6 days')
        """, (cid,))
        s7 = safe_float(cur.fetchone()["s7"])

        # previous 7 days (7-13 days ago)
        cur.execute("""
            SELECT COALESCE(SUM(e.amount),0) as p7
            FROM expenses e
            JOIN subcategories s ON s.id = e.subcategory_id
            WHERE s.category_id = ? AND DATE(e.date) BETWEEN DATE('now','-13 days') AND DATE('now','-7 days')
        """, (cid,))
        p7 = safe_float(cur.fetchone()["p7"])

        # total spent (authoritative: sum of subcategory amounts if expenses empty)
        cur.execute("SELECT COALESCE(SUM(amount),0) as total_spent FROM subcategories WHERE category_id = ?", (cid,))
        total_spent_cat = safe_float(cur.fetchone()["total_spent"])

        # growth ratio (s7 vs p7)
        growth_ratio = None
        if p7 > 0:
            growth_ratio = s7 / p7
        elif s7 > 0 and p7 == 0:
            growth_ratio = float('inf')

        category_trends.append({
            "id": cid,
            "name": cname,
            "assigned": assigned,
            "spent": total_spent_cat,
            "last7": s7,
            "prev7": p7,
            "growth_ratio": growth_ratio
        })

        # data for bar chart
        category_bar_labels.append(cname)
        category_assigned.append(assigned)
        category_spent.append(total_spent_cat)

    # pick fastest growing (largest growth_ratio >1)
    fastest = None
    best_ratio = 1.0
    for t in category_trends:
        r = t["growth_ratio"]
        if r is None:
            continue
        # treat inf as very large
        score = float('inf') if not isfinite(r) else r
        if score > best_ratio:
            best_ratio = score
            fastest = t

    # --- health scores (0-100) simple heuristics
    health_scores = []
    for t in category_trends:
        assigned = t["assigned"]
        spent = t["spent"]
        if assigned <= 0:
            remaining_pct = None
            score = 50
        else:
            remaining_pct = max(0.0, (assigned - spent) / assigned * 100.0)
            # Score favors higher remaining_pct and penalizes recent spikes
            # Base on remaining_pct
            score = int(max(0, min(100, remaining_pct)))
            # penalize growth spikes
            if t["growth_ratio"] and t["growth_ratio"] != float('inf'):
                if t["growth_ratio"] > 1.25:
                    score = max(0, score - 10)
                if t["growth_ratio"] > 1.5:
                    score = max(0, score - 10)
            elif t["growth_ratio"] == float('inf'):
                score = max(0, score - 5)

        health_scores.append({
            "name": t["name"],
            "assigned": assigned,
            "spent": spent,
            "remaining_pct": None if remaining_pct is None else round(remaining_pct, 1),
            "score": score
        })

    # --- suggestions: propose moves from healthy categories to risky ones
    suggestions = []
    # find low-score categories
    low = sorted([h for h in health_scores if h["assigned"]>0], key=lambda x: x["score"])
    high = sorted([h for h in health_scores if h["assigned"]>0], key=lambda x: -x["score"])
    if low and high:
        # suggest moving small amount from best to worst
        worst = low[0]
        best = high[0]
        # compute reasonable transfer: 5% of best assigned
        transfer = int(max(1, round(best["assigned"] * 0.05)))
        suggestions.append(f"💡 Consider moving ₨{transfer} from <b>{best['name']}</b> to <b>{worst['name']}</b> to avoid shortfall.")

    # General advice based on global prediction
    advice = []
    if days_until_zero is not None and days_until_zero <= 14:
        advice.append(f"🚨 At current pace, your budget may run out in about {int(days_until_zero)} day(s). Review major categories.")
    elif days_until_zero is not None:
        advice.append(f"📈 At current pace, funds will last ~{int(days_until_zero)} day(s).")

    if fastest:
        if fastest["growth_ratio"] == float('inf'):
            advice.append(f"🔥 Spending in <b>{fastest['name']}</b> started recently and is high this week.")
        else:
            pct = round((fastest["growth_ratio"] - 1.0) * 100, 1)
            if pct > 5:
                advice.append(f"⚠ <b>{fastest['name']}</b> spending rose by {pct}% vs previous week.")

    if total_spent == 0:
        advice.append("✨ No spending tracked yet; useful insights will appear as you add entries.")

    # Fallback if nothing
    if not suggestions and not advice:
        advice.append("😊 Your spending is stable. Keep it up!")

    return {
        "avg_daily_spend": round(avg_daily_spend, 2),
        "days_until_zero": None if days_until_zero is None else round(days_until_zero, 1),
        "fastest_growing": fastest,
        "health_scores": health_scores,
        "suggestions": suggestions,
        "advice": advice,
        "category_bar": {
            "labels": category_bar_labels,
            "assigned": category_assigned,
            "spent": category_spent
        }
    }

# ----------------------------
# Enhanced dashboard route with AI Insights + redesigned data for template
# ----------------------------
@app.route('/')
def dashboard():
    conn = get_db_connection()
    cur = conn.cursor()

    # totals
    cur.execute("SELECT COALESCE(SUM(assigned), 0) FROM categories")
    total_budget = safe_float(cur.fetchone()[0])

    cur.execute("SELECT COALESCE(SUM(amount), 0) FROM subcategories")
    total_spent = safe_float(cur.fetchone()[0])

    remaining = total_budget - total_spent

    # generate AI insights (uses DB)
    ai = generate_ai_insights(conn, total_budget, total_spent, remaining)

    # close connection (generate_ai_insights reused it, safe to close here)
    conn.close()
    if remaining<total_budget*0.09:
        add_notification("Warning! Your remaining Budget is below 8%")
    # pass to template
    return render_template("Dashboard.html",
                           total_budget=round(total_budget, 2),
                           total_spent=round(total_spent, 2),
                           remaining=round(remaining, 2),
                           ai_insights=ai)
# --- Totals API for live updates (returns JSON) ---
@app.route('/totals')
def totals_api():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(assigned), 0) FROM categories")
    total_budget = cur.fetchone()[0] or 0
    cur.execute("SELECT COALESCE(SUM(amount), 0) FROM subcategories")
    total_spent = cur.fetchone()[0] or 0
    conn.close()
    remaining = total_budget - total_spent
    return jsonify({
        "total_budget": round(total_budget, 2),
        "total_spent": round(total_spent, 2),
        "remaining": round(remaining, 2)
    })

def get_db_connection():
    conn = sqlite3.connect("finance.db")
    conn.row_factory = sqlite3.Row
    return conn

def compute_remaining(category_id, assigned):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT SUM(amount) as total FROM subcategories WHERE category_id = ?", (category_id,))
    row = cur.fetchone()
    sub_sum = row["total"] or 0
    conn.close()
    return assigned - sub_sum




@app.route("/expenses")
def expenses():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM categories ORDER BY id")
    cats = cur.fetchall()

    grouped = []
    for c in cats:
        cur.execute("SELECT * FROM subcategories WHERE category_id = ? ORDER BY id", (c["id"],))
        subs = cur.fetchall()
        assigned = c["assigned"] or 0
        sub_sum = sum([s["amount"] or 0 for s in subs])
        remaining = assigned - sub_sum
        grouped.append({
            "id": c["id"],
            "name": c["name"],
            "assigned": assigned,
            "remaining": remaining,
            "subcategories": [dict(s) for s in subs]
        })

    conn.close()
    return render_template("expenses.html", grouped=grouped)


@app.route("/add_category", methods=["POST"])
def add_category():
    name = request.form.get("name", "").strip() or "New Category"
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO categories (name, assigned) VALUES (?, ?)", (name, 0))
    cat_id = cur.lastrowid
    conn.commit()
    conn.close()
    return redirect("/expenses")


@app.route("/add_subcategory/<int:category_id>", methods=["POST"])
def add_subcategory(category_id):
    name = request.form.get("name", "").strip() or "New item"
    amount_raw = request.form.get("amount", "0")
    try:
        amount = float(amount_raw)
    except:
        amount = 0.0

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO subcategories (category_id, name, amount) VALUES (?, ?, ?)",
                (category_id, name, amount))
    sub_id = cur.lastrowid

    # return updated category data
    cur.execute("SELECT * FROM categories WHERE id = ?", (category_id,))
    cat = cur.fetchone()
    cur.execute("SELECT * FROM subcategories WHERE category_id = ? ORDER BY id", (category_id,))
    subs = [dict(x) for x in cur.fetchall()]
    assigned = cat["assigned"] or 0
    remaining = compute_remaining(category_id, assigned)
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "subcategory": {"id": sub_id, "category_id": category_id, "name": name, "amount": amount},
        "category": {"id": category_id, "assigned": assigned, "remaining": remaining, "subcategories": subs}
    })


# ---------- Update inline edits (category or subcategory) ----------
@app.route("/update", methods=["POST"])
def update():
    data = request.get_json()
    item_type = data.get("type")        # 'category' or 'subcategory'
    item_id = data.get("id")
    field = data.get("field")           # 'name' or 'assigned' or 'amount'
    value = data.get("value")

    conn = get_db_connection()
    cur = conn.cursor()

    if item_type == "category":
        if field == "name":
            cur.execute("UPDATE categories SET name = ? WHERE id = ?", (value, item_id))
        elif field == "assigned":
            try:
                assigned = float(value)
            except:
                assigned = 0.0
            cur.execute("UPDATE categories SET assigned = ? WHERE id = ?", (assigned, item_id))
    elif item_type == "subcategory":
     if field == "name":
        cur.execute("UPDATE subcategories SET name = ? WHERE id = ?", (value, item_id))
     elif field == "amount":
        try:
            amount = float(value)
        except:
            amount = 0.0

        # Get parent category and its budget
        cur.execute("SELECT category_id FROM subcategories WHERE id = ?", (item_id,))
        r = cur.fetchone()
        if r:
            cat_id = r["category_id"]
            cur.execute("SELECT assigned FROM categories WHERE id = ?", (cat_id,))
            cat_budget = cur.fetchone()[0] or 0

            # Current total used in that category (excluding this one)
            cur.execute("SELECT SUM(amount) FROM subcategories WHERE category_id = ? AND id != ?", (cat_id, item_id))
            used = cur.fetchone()[0] or 0

            remaining = cat_budget - used
            final_amount = min(amount, remaining)
            cur.execute("UPDATE subcategories SET amount = ? WHERE id = ?", (final_amount, item_id))
        else:
            cur.execute("UPDATE subcategories SET amount = ? WHERE id = ?", (amount, item_id))

    else:
        conn.close()
        return jsonify({"success": False, "error": "invalid type"})

    conn.commit()

    # Return updated category summary to update UI
    if item_type == "category":
        cat_id = item_id
    else:
        # find parent category
        cur.execute("SELECT category_id FROM subcategories WHERE id = ?", (item_id,))
        r = cur.fetchone()
        cat_id = r["category_id"] if r else None

    if cat_id:
        cur.execute("SELECT * FROM categories WHERE id = ?", (cat_id,))
        cat = cur.fetchone()
        cur.execute("SELECT * FROM subcategories WHERE category_id = ? ORDER BY id", (cat_id,))
        subs = [dict(x) for x in cur.fetchall()]
        assigned = cat["assigned"] or 0
        remaining = compute_remaining(cat_id, assigned)
    else:
        assigned = 0
        remaining = 0
        subs = []

    conn.close()
    return jsonify({
        "success": True,
        "category": {"id": cat_id, "assigned": assigned, "remaining": remaining, "subcategories": subs}
    })


# ---------- Delete endpoints ----------
@app.route("/delete_sub/<int:sub_id>", methods=["POST"])
def delete_sub(sub_id):
    conn = get_db_connection()
    cur = conn.cursor()
    # find category id for response
    cur.execute("SELECT category_id FROM subcategories WHERE id = ?", (sub_id,))
    r = cur.fetchone()
    cat_id = r["category_id"] if r else None
    cur.execute("DELETE FROM subcategories WHERE id = ?", (sub_id,))
    conn.commit()
    # return updated category summary
    if cat_id:
        cur.execute("SELECT * FROM categories WHERE id = ?", (cat_id,))
        cat = cur.fetchone()
        cur.execute("SELECT * FROM subcategories WHERE category_id = ? ORDER BY id", (cat_id,))
        subs = [dict(x) for x in cur.fetchall()]
        assigned = cat["assigned"] or 0
        remaining = compute_remaining(cat_id, assigned)
    else:
        subs = []
        assigned = 0
        remaining = 0
    conn.close()
    return jsonify({"success": True, "category": {"id": cat_id, "assigned": assigned, "remaining": remaining, "subcategories": subs}})


@app.route("/delete_cat/<int:cat_id>", methods=["POST"])
def delete_cat(cat_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    cur.execute("DELETE FROM subcategories WHERE category_id = ?", (cat_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ---------- Insights ----------
import math
from datetime import datetime

def generate_ultra_insights(conn):
    """
    Returns a dict containing:
      - category_bar {labels, assigned, spent}
      - categories: list of {name, assigned, spent, remaining, remaining_pct, risk_score, days_left}
      - predictions: overall days_until_zero (based on avg daily spend)
      - fastest_growing: category with biggest last7/prev7 ratio
      - recommendations: list of actionable strings
      - badges: list of short achievement strings
    """
    cur = conn.cursor()

    # --- load categories and subcategory sums ---
    cur.execute("SELECT id, name, COALESCE(assigned,0) AS assigned FROM categories")
    cats = cur.fetchall()

    labels = []
    assigned_arr = []
    spent_arr = []

    categories = []  # will hold detailed per-category objects
    for c in cats:
        cid = c["id"]
        name = c["name"]
        assigned = float(c["assigned"] or 0)
        labels.append(name)
        assigned_arr.append(assigned)

        # ✅ Spent is always subcategories.amount (single source of truth)
        cur.execute(
          "SELECT COALESCE(SUM(amount),0) AS total FROM subcategories WHERE category_id = ?",
           (cid,)
        )
        spent = float(cur.fetchone()["total"] or 0)


        spent_arr.append(spent)

        remaining = assigned - spent
        remaining_pct = None
        if assigned > 0:
            remaining_pct = max(0.0, remaining / assigned * 100.0)

        # last7 vs prev7 growth
        cur.execute("""
            SELECT COALESCE(SUM(e.amount),0) as last7
            FROM expenses e
            JOIN subcategories s ON s.id = e.subcategory_id
            WHERE s.category_id = ? AND DATE(e.date) >= DATE('now','-6 days')
        """, (cid,))
        last7 = float(cur.fetchone()["last7"] or 0)

        cur.execute("""
            SELECT COALESCE(SUM(e.amount),0) as prev7
            FROM expenses e
            JOIN subcategories s ON s.id = e.subcategory_id
            WHERE s.category_id = ? AND DATE(e.date) BETWEEN DATE('now','-13 days') AND DATE('now','-7 days')
        """, (cid,))
        prev7 = float(cur.fetchone()["prev7"] or 0)

        growth_ratio = None
        if prev7 > 0:
            growth_ratio = last7 / prev7
        elif last7 > 0 and prev7 == 0:
            growth_ratio = float('inf')

        # risk score: combine remaining_pct and growth spike
        if assigned <= 0:
            risk_score = 50
        else:
            base = remaining_pct if remaining_pct is not None else 0
            risk_score = int(max(0, min(100, base)))
            if growth_ratio and math.isfinite(growth_ratio):
                if growth_ratio > 1.25:
                    risk_score = max(0, risk_score - 12)
                if growth_ratio > 1.5:
                    risk_score = max(0, risk_score - 12)
            elif growth_ratio == float('inf'):
                risk_score = max(0, risk_score - 8)

        # days left estimate for category (based on avg daily spend in that category over last 30 days)
        cur.execute("""
            SELECT COALESCE(SUM(e.amount),0) as total30, COUNT(DISTINCT DATE(e.date)) as days_recorded
            FROM expenses e
            JOIN subcategories s ON s.id = e.subcategory_id
            WHERE s.category_id = ? AND DATE(e.date) >= DATE('now','-29 days')
        """, (cid,))
        row = cur.fetchone()
        total30 = float(row["total30"] or 0)
        days_recorded = int(row["days_recorded"] or 0)
        avg_daily_cat = (total30 / days_recorded) if days_recorded > 0 else (last7 / 7.0 if last7 > 0 else 0)
        days_left = None
        if avg_daily_cat > 0:
            days_left = max(0, int((assigned - spent) / avg_daily_cat)) if (assigned - spent) > 0 else 0

        categories.append({
            "id": cid,
            "name": name,
            "assigned": round(assigned,2),
            "spent": round(spent,2),
            "remaining": round(remaining,2),
            "remaining_pct": None if remaining_pct is None else round(remaining_pct,1),
            "growth_last7": round(last7,2),
            "growth_prev7": round(prev7,2),
            "growth_ratio": None if growth_ratio is None else (float('inf') if growth_ratio==float('inf') else round(growth_ratio,2)),
            "risk_score": risk_score,
            "days_left": days_left
        })

    # --- overall totals & prediction (global) ---
    cur.execute("SELECT COALESCE(SUM(assigned),0) as total_assigned FROM categories")
    total_budget = float(cur.fetchone()["total_assigned"] or 0)
    cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM subcategories")
    total_spent = float(cur.fetchone()["total"] or 0)
    total_spent = float((cur.fetchone() or {}).get("total_spent") or 0) if cur.fetchone() else 0

    # a safer way: compute total_spent via subcategories if expenses table empty
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='expenses'")
    if cur.fetchone():
        cur.execute("SELECT COALESCE(SUM(e.amount),0) as total FROM expenses e")
        total_spent = float(cur.fetchone()["total"] or 0)
    else:
        cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM subcategories")
        total_spent = float(cur.fetchone()["total"] or 0)

    remaining_total = total_budget - total_spent

    # avg daily spend (last 30 days)
    cur.execute("SELECT COALESCE(SUM(amount),0) as total30 FROM expenses WHERE DATE(date) >= DATE('now','-29 days')")
    total30_all = float(cur.fetchone()["total30"] or 0)
    avg_daily = total30_all / 30.0

    days_until_zero = None
    if avg_daily > 0:
        days_until_zero = remaining_total / avg_daily
        if days_until_zero < 0:
            days_until_zero = 0

    # fastest growing category (use growth ratio from categories list)
    fastest = None
    best_ratio = 1.0
    for catobj in categories:
        r = catobj["growth_ratio"]
        if r is None:
            continue
        score = float('inf') if (r == float('inf')) else r
        if score > best_ratio:
            best_ratio = score
            fastest = catobj

    # Recommendations and warnings
    recommendations = []
    warnings = []

    # global warnings
    if total_budget > 0:
        remaining_pct_total = max(0.0, remaining_total / total_budget * 100.0)
        if remaining_pct_total <= 10:
            warnings.append(f"Your total remaining budget is low ({int(remaining_pct_total)}%). Consider reducing variable spending.")
        elif remaining_pct_total <= 20:
            recommendations.append(f"Total remaining is {int(remaining_pct_total)}% — keep an eye on discretionary expenses.")

    # per-category warnings & suggestions
    for catobj in categories:
        if catobj["risk_score"] <= 40:
            warnings.append(f"{catobj['name']} is at risk (score {catobj['risk_score']}/100). Consider reducing spend or moving funds.")
        elif catobj["risk_score"] <= 60:
            recommendations.append(f"{catobj['name']} shows rising usage — monitor this category this week.")

    # make a small transfer suggestion: move 5% from healthiest to riskiest if possible
    healthy = sorted([c for c in categories if c["assigned"]>0], key=lambda x: -x["risk_score"])
    risky = sorted([c for c in categories if c["assigned"]>0], key=lambda x: x["risk_score"])
    if healthy and risky:
        best = healthy[0]
        worst = risky[0]
        if best["risk_score"] - worst["risk_score"] >= 20 and best["assigned"] > 0:
            transfer_amt = max(1, int(round(best["assigned"] * 0.05)))
            recommendations.append(f"Consider moving ₨{transfer_amt} from {best['name']} to {worst['name']} to prevent shortfalls.")

    # badges (simple gamification)
    badges = []
    if remaining_total > total_budget * 0.3:
        badges.append("🌟 Stable Spender")
    if remaining_total > total_budget * 0.6:
        badges.append("🏆 Great Saver")
    # streak / pattern badges could be added later when we store history
    if not categories:
        badges.append("📝 Add categories to get insights")

    # assemble category_bar data
    category_bar = {
        "labels": labels,
        "assigned": [round(x,2) for x in assigned_arr],
        "spent": [round(x,2) for x in spent_arr]
    }

    return {
        "category_bar": category_bar,
        "categories": categories,
        "total_budget": round(total_budget,2),
        "total_spent": round(total_spent,2),
        "remaining_total": round(remaining_total,2),
        "avg_daily": round(avg_daily,2),
        "days_until_zero": None if days_until_zero is None else int(days_until_zero),
        "fastest_growing": fastest,
        "recommendations": recommendations,
        "warnings": warnings,
        "badges": badges
    }


@app.route('/insights')
def insights():
    conn = get_db_connection()
    try:
        ai = generate_ultra_insights(conn)
        # pass chart arrays and insights to template
        return render_template("insights.html",
                               ai_insights=ai)
    finally:
        conn.close()


DB = "finance.db"
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


@app.before_request
def load_settings():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM settings LIMIT 1")
    g.settings = cursor.fetchone()
    conn.close()

@app.context_processor
def inject_settings():
    return dict(settings=g.settings)

@app.route("/settings", methods=["GET", "POST"])
def settings():
    conn = get_db()
    cursor = conn.cursor()

    if request.method == "POST":
      username = request.form.get("username")
      about = request.form.get("about")
      currency = request.form.get("currency")
      theme = request.form.get("theme")

      cursor.execute("""
         UPDATE settings
         SET username = ?, about = ?, currency = ?, theme = ?
         WHERE id = 1
         """, (username, about, currency, theme))
      conn.commit()
      return redirect(url_for("settings"))
    cursor.execute("SELECT * FROM settings LIMIT 1")
    settings = cursor.fetchone()
    conn.close()
    return render_template("settings.html", settings=settings)


def add_notification(message):
    
    
    today_str = datetime.now().date().isoformat()
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM notifications WHERE message = ? AND DATE(date) = ?", (message, today_str))
    existing = cur.fetchone()

    if not existing:
        cur.execute("INSERT INTO notifications (message) VALUES (?)", (message,))
        conn.commit()

    conn.close()


@app.route("/notifications")
def notifications():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, message, date, is_read FROM notifications ORDER BY date DESC")
    notifications = [dict(row) for row in cur.fetchall()]
    return render_template("notifications.html", notifications=notifications)


@app.route("/mark_read/<int:id>")
def mark_read(id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("notifications"))

@app.route("/clear_notifications")
def clear_notifications():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM notifications")
    conn.commit()
    conn.close()
    return redirect(url_for("notifications"))


if __name__ == "__main__":
    app.run(debug=True,host='0.0.0.0', port=5000)