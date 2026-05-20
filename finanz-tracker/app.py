import sqlite3
import os
import signal
import sys
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify

app = Flask(__name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "finanz.db")
PID_FILE = os.path.join(APP_DIR, "app.pid")

ACCOUNTS = [
    ("DKB Girokonto", "girokonto"),
    ("Tagesgeld Gemeinschaftskonto", "tagesgeld"),
    ("Tagesgeld Rosa", "tagesgeld"),
    ("Tagesgeld Janosch", "tagesgeld"),
    ("Depot Gemeinschaftskonto", "depot"),
    ("Depot TradeRepublic", "depot"),
    ("Depot Rosa", "depot"),
    ("Depot Janosch", "depot"),
]

CATEGORIES = [
    "Wohnen",
    "Lebensmittel",
    "Mobilität",
    "Versicherungen",
    "Freizeit",
    "Kinder",
    "Kleidung",
    "Sonstiges",
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS monthly_balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            balance REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (account_id) REFERENCES accounts(id),
            UNIQUE(account_id, year, month)
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    for name, acc_type in ACCOUNTS:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (name, type) VALUES (?, ?)",
            (name, acc_type),
        )
    conn.commit()
    conn.close()


@app.route("/")
def dashboard():
    conn = get_db()

    now = datetime.now()
    current_year = now.year
    current_month = now.month

    balances = conn.execute("""
        SELECT a.name, a.type, mb.balance, mb.year, mb.month
        FROM accounts a
        LEFT JOIN monthly_balances mb ON a.id = mb.account_id
            AND mb.year = ? AND mb.month = ?
        ORDER BY a.type, a.name
    """, (current_year, current_month)).fetchall()

    total_current = sum(row["balance"] or 0 for row in balances)

    if current_month == 1:
        prev_year, prev_month = current_year - 1, 12
    else:
        prev_year, prev_month = current_year, current_month - 1

    prev_balances = conn.execute("""
        SELECT COALESCE(SUM(mb.balance), 0) as total
        FROM accounts a
        LEFT JOIN monthly_balances mb ON a.id = mb.account_id
            AND mb.year = ? AND mb.month = ?
    """, (prev_year, prev_month)).fetchone()

    total_previous = prev_balances["total"] or 0
    change = total_current - total_previous

    recent_expenses = conn.execute("""
        SELECT * FROM expenses ORDER BY date DESC, id DESC LIMIT 10
    """).fetchall()

    monthly_expenses = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) as total
        FROM expenses
        WHERE substr(date, 1, 7) = ?
    """, (f"{current_year:04d}-{current_month:02d}",)).fetchone()

    conn.close()

    return render_template(
        "dashboard.html",
        balances=balances,
        total_current=total_current,
        total_previous=total_previous,
        change=change,
        recent_expenses=recent_expenses,
        monthly_expenses=monthly_expenses["total"],
        current_year=current_year,
        current_month=current_month,
        categories=CATEGORIES,
    )


@app.route("/balances", methods=["GET", "POST"])
def balances():
    conn = get_db()

    if request.method == "POST":
        year = int(request.form["year"])
        month = int(request.form["month"])

        accounts = conn.execute("SELECT * FROM accounts ORDER BY type, name").fetchall()
        for account in accounts:
            balance_str = request.form.get(f"balance_{account['id']}", "0")
            balance = float(balance_str.replace(",", ".")) if balance_str else 0

            conn.execute("""
                INSERT INTO monthly_balances (account_id, year, month, balance)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id, year, month)
                DO UPDATE SET balance = excluded.balance
            """, (account["id"], year, month, balance))

        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))

    now = datetime.now()
    year = int(request.args.get("year", now.year))
    month = int(request.args.get("month", now.month))

    accounts = conn.execute("""
        SELECT a.*, mb.balance
        FROM accounts a
        LEFT JOIN monthly_balances mb ON a.id = mb.account_id
            AND mb.year = ? AND mb.month = ?
        ORDER BY a.type, a.name
    """, (year, month)).fetchall()

    conn.close()

    return render_template(
        "balances.html",
        accounts=accounts,
        year=year,
        month=month,
    )


@app.route("/expenses", methods=["GET", "POST"])
def expenses():
    conn = get_db()

    if request.method == "POST":
        date = request.form["date"]
        amount = float(request.form["amount"].replace(",", "."))
        category = request.form["category"]
        description = request.form.get("description", "")

        conn.execute("""
            INSERT INTO expenses (date, amount, category, description)
            VALUES (?, ?, ?, ?)
        """, (date, amount, category, description))
        conn.commit()
        conn.close()
        return redirect(url_for("expenses"))

    page = int(request.args.get("page", 1))
    per_page = 25
    offset = (page - 1) * per_page

    total_count = conn.execute("SELECT COUNT(*) as cnt FROM expenses").fetchone()["cnt"]
    all_expenses = conn.execute("""
        SELECT * FROM expenses ORDER BY date DESC, id DESC
        LIMIT ? OFFSET ?
    """, (per_page, offset)).fetchall()

    conn.close()

    total_pages = (total_count + per_page - 1) // per_page

    return render_template(
        "expenses.html",
        expenses=all_expenses,
        categories=CATEGORIES,
        page=page,
        total_pages=total_pages,
    )


@app.route("/expenses/delete/<int:expense_id>", methods=["POST"])
def delete_expense(expense_id):
    conn = get_db()
    conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("expenses"))


@app.route("/charts")
def charts():
    return render_template("charts.html")


@app.route("/api/chart-data")
def chart_data():
    conn = get_db()

    history = conn.execute("""
        SELECT mb.year, mb.month, a.name, a.type, mb.balance
        FROM monthly_balances mb
        JOIN accounts a ON a.id = mb.account_id
        ORDER BY mb.year, mb.month, a.type, a.name
    """).fetchall()

    months_set = sorted(set((row["year"], row["month"]) for row in history))
    labels = [f"{m:02d}/{y}" for y, m in months_set]

    totals = []
    by_type = {"girokonto": [], "tagesgeld": [], "depot": []}

    for y, m in months_set:
        month_total = 0
        type_totals = {"girokonto": 0, "tagesgeld": 0, "depot": 0}
        for row in history:
            if row["year"] == y and row["month"] == m:
                month_total += row["balance"]
                type_totals[row["type"]] += row["balance"]
        totals.append(month_total)
        for t in by_type:
            by_type[t].append(type_totals[t])

    expense_data = conn.execute("""
        SELECT substr(date, 1, 7) as month, category, SUM(amount) as total
        FROM expenses
        GROUP BY substr(date, 1, 7), category
        ORDER BY month
    """).fetchall()

    expense_months = sorted(set(row["month"] for row in expense_data))
    expense_by_category = {}
    for row in expense_data:
        cat = row["category"]
        if cat not in expense_by_category:
            expense_by_category[cat] = {m: 0 for m in expense_months}
        expense_by_category[cat][row["month"]] = row["total"]

    conn.close()

    return jsonify({
        "balance": {
            "labels": labels,
            "totals": totals,
            "by_type": by_type,
        },
        "expenses": {
            "labels": expense_months,
            "by_category": {
                cat: [vals[m] for m in expense_months]
                for cat, vals in expense_by_category.items()
            },
        },
    })


if __name__ == "__main__":
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    init_db()
    print(f"Finanz-Tracker läuft auf http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
