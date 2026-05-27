import sqlite3
import os
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, jsonify

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "finanz.db"))

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

        CREATE TABLE IF NOT EXISTS balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (account_id) REFERENCES accounts(id),
            UNIQUE(account_id, date)
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
    today = date.today().isoformat()

    balances = conn.execute("""
        SELECT a.name, a.type, b.balance, b.date
        FROM accounts a
        LEFT JOIN balances b ON a.id = b.account_id
            AND b.date = (SELECT MAX(b2.date) FROM balances b2 WHERE b2.account_id = a.id)
        ORDER BY a.type, a.name
    """).fetchall()

    total_current = sum(row["balance"] or 0 for row in balances)

    prev_balances = conn.execute("""
        SELECT a.id, b.balance
        FROM accounts a
        LEFT JOIN balances b ON a.id = b.account_id
            AND b.date = (
                SELECT MAX(b2.date) FROM balances b2
                WHERE b2.account_id = a.id
                AND b2.date < (SELECT MAX(b3.date) FROM balances b3 WHERE b3.account_id = a.id)
            )
    """).fetchall()

    total_previous = sum(row["balance"] or 0 for row in prev_balances)
    change = total_current - total_previous

    recent_expenses = conn.execute("""
        SELECT * FROM expenses ORDER BY date DESC, id DESC LIMIT 10
    """).fetchall()

    now = datetime.now()
    monthly_expenses = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) as total
        FROM expenses
        WHERE substr(date, 1, 7) = ?
    """, (f"{now.year:04d}-{now.month:02d}",)).fetchone()

    conn.close()

    return render_template(
        "dashboard.html",
        balances=balances,
        total_current=total_current,
        total_previous=total_previous,
        change=change,
        recent_expenses=recent_expenses,
        monthly_expenses=monthly_expenses["total"],
        today=today,
        categories=CATEGORIES,
    )


@app.route("/balances", methods=["GET", "POST"])
def balances():
    conn = get_db()

    if request.method == "POST":
        entry_date = request.form["date"]

        accounts = conn.execute("SELECT * FROM accounts ORDER BY type, name").fetchall()
        for account in accounts:
            balance_str = request.form.get(f"balance_{account['id']}", "0")
            balance = float(balance_str.replace(",", ".")) if balance_str else 0

            conn.execute("""
                INSERT INTO balances (account_id, date, balance)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id, date)
                DO UPDATE SET balance = excluded.balance
            """, (account["id"], entry_date, balance))

        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))

    entry_date = request.args.get("date", date.today().isoformat())

    accounts = conn.execute("""
        SELECT a.*, b.balance
        FROM accounts a
        LEFT JOIN balances b ON a.id = b.account_id AND b.date = ?
        ORDER BY a.type, a.name
    """, (entry_date,)).fetchall()

    conn.close()

    return render_template(
        "balances.html",
        accounts=accounts,
        entry_date=entry_date,
    )


@app.route("/balances/history")
def balance_history():
    conn = get_db()

    entries = conn.execute("""
        SELECT b.date, a.name, a.type, b.balance
        FROM balances b
        JOIN accounts a ON a.id = b.account_id
        ORDER BY b.date DESC, a.type, a.name
    """).fetchall()

    dates = []
    grouped = {}
    for row in entries:
        d = row["date"]
        if d not in grouped:
            grouped[d] = {"date": d, "accounts": [], "total": 0}
            dates.append(d)
        grouped[d]["accounts"].append(row)
        grouped[d]["total"] += row["balance"]

    conn.close()

    return render_template(
        "balance_history.html",
        dates=dates,
        grouped=grouped,
    )


@app.route("/expenses", methods=["GET", "POST"])
def expenses():
    conn = get_db()

    if request.method == "POST":
        exp_date = request.form["date"]
        amount = float(request.form["amount"].replace(",", "."))
        category = request.form["category"]
        description = request.form.get("description", "")

        conn.execute("""
            INSERT INTO expenses (date, amount, category, description)
            VALUES (?, ?, ?, ?)
        """, (exp_date, amount, category, description))
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
        SELECT b.date, a.name, a.type, b.balance
        FROM balances b
        JOIN accounts a ON a.id = b.account_id
        ORDER BY b.date, a.type, a.name
    """).fetchall()

    dates_set = sorted(set(row["date"] for row in history))
    labels = [d[5:] + "/" + d[:4] for d in dates_set]

    totals = []
    by_type = {"girokonto": [], "tagesgeld": [], "depot": []}

    for d in dates_set:
        day_total = 0
        type_totals = {"girokonto": 0, "tagesgeld": 0, "depot": 0}
        for row in history:
            if row["date"] == d:
                day_total += row["balance"]
                type_totals[row["type"]] += row["balance"]
        totals.append(day_total)
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
    init_db()
    print("Finanz-Tracker laeuft auf http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
