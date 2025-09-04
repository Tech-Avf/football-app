from flask import Flask, render_template, request, redirect, url_for, jsonify
import json, os, time
from datetime import datetime
import pytz
import os
import requests
import base64
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

LOCK_DURATION = 60  # giây
DB_FILE = "db.json"
ANNOUNCE_FILE = "data/announcements.json"

# ================== UTILITIES ==================
def load_announcements():
    if os.path.exists(ANNOUNCE_FILE):
        with open(ANNOUNCE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_announcements(data):
    with open(ANNOUNCE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# Bảo đảm db.json tồn tại
if not os.path.exists(DB_FILE):
    initial_data = {
        "schedules": [],
        "players": []
    }
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(initial_data, f, ensure_ascii=False, indent=2)

def load_data():
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_schedule(date):
    data = load_data()
    for s in data["schedules"]:
        if s["id"] == date:
            return s
    return None

def get_players_for_schedule(date):
    data = load_data()
    schedule = get_schedule(date)
    if not schedule:
        return []
    players = data["players"]
    result = []
    for p in players:
        pid = str(p["id"])
        status = schedule["status"].get(pid)
        if status:
            result.append({
                "id": p["id"],
                "name": p["name"],
                "number": p["number"],
                "position": p["position"],
                "state": status.get("state", "")
            })
    return result

# ================== TEMPLATE FILTER & HELPERS ==================
def _format_date_with_weekday(date_str: str) -> str:
    """
    Định dạng 'YYYY-MM-DD' -> 'Thứ ..., dd/mm/YYYY'
    Không phụ thuộc locale hệ điều hành (tránh lỗi font).
    """
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekday_vi = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
        return f"{weekday_vi[dt.weekday()]} | {dt.strftime('%d/%m/%Y')}"
    except Exception:
        return date_str

# đăng ký filter
app.jinja_env.filters["format_date_with_weekday"] = _format_date_with_weekday

@app.context_processor
def inject_helpers():
    # Giữ helper cũ + đưa hàm format vào context cho template cũ gọi kiểu function nếu còn dùng
    def get_line_color(pos):
        if pos.startswith("CB") or pos in ["LB", "RB"]:
            return "DEF"
        elif pos in ["GK"]:
            return "GK"
        elif pos in ["CM", "LM", "RM", "DM", "AM"]:
            return "MID"
        elif pos in ["ST", "CF", "SS", "LW", "RW"]:
            return "FWD"
        else:
            return ""
    return dict(
        get_line_color=get_line_color,
        format_date_with_weekday=_format_date_with_weekday
    )

# ================== INDEX ==================
@app.route("/")
def index():
    data = load_data()
    admin_mode = request.args.get("admin") == "1"

    sorted_schedules = sorted(
        data["schedules"],
        key=lambda s: datetime.strptime(f"{s['date']} {s['time']}", "%Y-%m-%d %H:%M"),
        reverse=True
    )
    newest_id = sorted_schedules[0]["id"] if sorted_schedules else None

    # Thông báo: tách 2 loại
    anns = load_announcements()
    # tương thích ngược: nếu 'is_banner' có thì coi như is_scrolling
    for a in anns:
        if a.get("is_banner") and "is_scrolling" not in a:
            a["is_scrolling"] = True

    banner_announcement = next((a for a in anns if a.get("is_scrolling")), None)  # chạy nổi
    popup_announcement  = next((a for a in anns if a.get("is_popup")), None)      # popup

    return render_template(
        "index.html",
        schedules=sorted_schedules,
        admin=admin_mode,
        newest_id=newest_id,
        banner_announcement=banner_announcement,
        popup_announcement=popup_announcement
    )

# ================== REGISTER ==================
@app.route("/register/<date>", methods=["GET", "POST"])
def register(date):
    import time as _time

    data = load_data()
    schedule = next((s for s in data["schedules"] if s["id"] == date), None)
    if not schedule:
        return f"Không tìm thấy ngày {date}", 404

    players = sorted(data["players"], key=lambda p: p["order"])
    statuses = schedule["status"]
    admin_mode = request.args.get("admin") == "1"
    vn_tz = pytz.timezone("Asia/Ho_Chi_Minh")
    now = _time.time()

    locked_at_ts = schedule.get("locked_at", 0)
    if locked_at_ts:
        locked_at_utc = datetime.utcfromtimestamp(locked_at_ts).replace(tzinfo=pytz.utc)
        locked_at_vn = locked_at_utc.astimezone(vn_tz)
        locked_at_global = int(locked_at_vn.timestamp())
    else:
        locked_at_global = 0

    is_locked = not admin_mode and now > locked_at_global

    if request.method == "POST":
        for player in players:
            pid = str(player["id"])
            current = statuses.get(pid, {})
            lock_time = current.get("locked_at", 0)
            is_player_locked = lock_time and (now - lock_time > LOCK_DURATION)

            if not admin_mode and is_player_locked:
                continue

            state = request.form.get(f"state_{pid}", current.get("state", ""))
            note = request.form.get(f"note_{pid}", current.get("note", ""))
            reason = request.form.get(f"reason_{pid}", current.get("reason", ""))

            if not admin_mode:
                if state in ["join", "busy"]:
                    if not lock_time:
                        lock_time = now
                elif state == "":
                    lock_time = 0

            statuses[pid] = {
                "state": state,
                "note": note,
                "reason": reason,
                "locked_at": lock_time or 0
            }

        save_data(data)
        return redirect(url_for("register", date=date) + ("?admin=1" if admin_mode else ""))

    if locked_at_global:
        locked_datetime_str = datetime.fromtimestamp(locked_at_global).astimezone(vn_tz).strftime("%Y-%m-%d %H:%M")
    else:
        locked_datetime_str = "Chưa đặt"

    return render_template(
        "register.html",
        players=players,
        schedule=schedule,
        statuses=statuses,
        lock_duration=LOCK_DURATION,
        admin=admin_mode,
        now=now,
        locked_at_global=locked_at_global,
        is_locked=is_locked,
        locked_datetime_str=locked_datetime_str
    )

@app.route("/register/<date>/remove/<int:player_id>", methods=["POST"])
def remove_player_from_session(date, player_id):
    admin_mode = request.args.get("admin") == "1"
    if not admin_mode:
        return "Không có quyền xoá", 403

    data = load_data()
    schedule = next((s for s in data["schedules"] if s["id"] == date), None)
    if not schedule:
        return f"Không tìm thấy buổi đá {date}", 404

    pid = str(player_id)
    if pid in schedule["status"]:
        del schedule["status"][pid]

    save_data(data)
    return redirect(url_for("register", date=date) + "?admin=1")

@app.route("/register/<date>/add/<int:player_id>", methods=["POST"])
def add_player_to_session(date, player_id):
    admin_mode = request.args.get("admin") == "1"
    if not admin_mode:
        return "Không có quyền thêm", 403

    data = load_data()
    player = next((p for p in data["players"] if p["id"] == player_id), None)
    if not player:
        return f"Không tìm thấy cầu thủ", 404

    schedule = next((s for s in data["schedules"] if s["id"] == date), None)
    if not schedule:
        return f"Không tìm thấy buổi đá", 404

    pid = str(player_id)
    if pid not in schedule["status"]:
        schedule["status"][pid] = {
            "state": "",
            "note": "",
            "reason": "",
            "locked_at": 0
        }

    save_data(data)
    return redirect(url_for("register", date=date) + "?admin=1")

# ================== CREATE SCHEDULE ==================
@app.route("/create", methods=["GET", "POST"])
def create():
    data = load_data()
    if request.method == "POST":
        date = request.form.get("date")
        time_ = request.form.get("time")
        field = request.form.get("location")
        map_link = request.form.get("map_link")
        locked_at_str = request.form.get("locked_at")

        vn_tz = pytz.timezone("Asia/Ho_Chi_Minh")
        locked_at_naive = datetime.fromisoformat(locked_at_str)
        locked_at_local = vn_tz.localize(locked_at_naive)
        locked_at = int(locked_at_local.astimezone(pytz.utc).timestamp())

        status = {
            str(player["id"]): {
                "state": "",
                "note": "",
                "reason": "",
                "locked_at": 0
            } for player in data["players"]
        }

        data["schedules"].append({
            "id": date,
            "date": date,
            "time": time_,
            "field": field,
            "map": map_link,
            "locked_at": locked_at,
            "status": status
        })

        save_data(data)
        return redirect(url_for("index"))

    return render_template("create.html")

# ================== ADMIN PLAYERS ==================
@app.route("/admin/players", methods=["GET", "POST"])
def admin_players():
    data = load_data()
    players = data["players"]

    if request.method == "POST":
        updated_players = []
        for player in players:
            pid = str(player["id"])
            if request.form.get(f"delete_{pid}"):
                continue
            updated_players.append({
                "id": player["id"],
                "name": request.form.get(f"name_{pid}", ""),
                "position": ",".join(request.form.getlist(f"position_{pid}[]")),
                "number": int(request.form.get(f"number_{pid}", 0)),
                "order": int(request.form.get(f"order_{pid}", 0))
            })

        if request.form.get("new_name"):
            new_id = 1
            if updated_players:
                new_id = max([p["id"] for p in updated_players]) + 1
            new_player = {
                "id": new_id,
                "name": request.form["new_name"],
                "position": ",".join(request.form.getlist("new_position[]")),
                "number": int(request.form.get("new_number", 0)),
                "order": int(request.form.get("new_order", 0))
            }
            updated_players.append(new_player)

            for schedule in data["schedules"]:
                schedule["status"][str(new_id)] = {
                    "state": "",
                    "note": "",
                    "reason": "",
                    "locked_at": 0
                }

        data["players"] = updated_players
        save_data(data)
        return redirect(url_for("admin_players"))

    return render_template("admin_players.html", players=players)

# ================== DELETE SCHEDULE ==================
@app.route("/delete/<date>", methods=["POST"])
def delete_schedule(date):
    admin_mode = request.args.get("admin") == "1"
    if not admin_mode:
        return "Không có quyền xoá", 403
    data = load_data()
    data["schedules"] = [s for s in data["schedules"] if s["id"] != date]
    save_data(data)
    return redirect(url_for("index") + "?admin=1")

# ================== COACH MODE ==================
@app.route("/coach/<date>")
def coach_mode(date):
    schedule = get_schedule(date)
    if not schedule:
        return "Không tìm thấy buổi đá", 404

    data = load_data()
    players = data["players"]
    statuses = schedule.get("status", {})

    joined_players = [
        p for p in players
        if str(p["id"]) in statuses and statuses[str(p["id"])]["state"] == "join"
    ]

    players_by_position = {}
    for player in joined_players:
        if player.get("position"):
            for pos in player["position"].split(","):
                pos = pos.strip().upper()
                players_by_position.setdefault(pos, []).append({
                    "id": player["id"],
                    "name": player["name"],
                    "number": player["number"],
                    "position": player["position"]
                })

    return render_template(
        "coach_mode.html",
        schedule=schedule,
        players_by_position=players_by_position,
        all_joined_players=joined_players
    )

# ================== STATIC PAGES ==================
@app.route("/about.html")
def about():
    return render_template("about.html")

# ================== ANNOUNCEMENTS (LIST) ==================
@app.route('/announcements')
def announcements():
    anns = load_announcements()
    is_admin = request.args.get('admin') == '1'
    return render_template('announcements.html', announcements=anns, is_admin=is_admin)

@app.route('/add_announcement', methods=['POST'])
def add_announcement():
    if request.args.get('admin') != '1':
        return "Không có quyền", 403

    anns = load_announcements()
    new_item = {
        "title": request.form['title'],
        "content": request.form['content'],
        "date": datetime.now().strftime("%d/%m/%Y"),
        # hai tuỳ chọn mới
        "is_scrolling": ('is_scrolling' in request.form) or ('is_banner' in request.form),  # tương thích ngược
        "is_popup": 'is_popup' in request.form
    }
    anns.insert(0, new_item)
    save_announcements(anns)
    return redirect(url_for('announcements', admin=1))

@app.route('/delete_announcement/<int:index>', methods=['POST'])
def delete_announcement(index):
    if request.args.get('admin') != '1':
        return "Không có quyền", 403
    anns = load_announcements()
    if 0 <= index < len(anns):
        del anns[index]
        save_announcements(anns)
    return redirect(url_for('announcements', admin=1))

@app.route('/toggle_banner/<int:index>', methods=['POST'])
def toggle_banner(index):
    """Giữ tương thích: toggle 'is_scrolling' (trước đây là is_banner)."""
    if request.args.get('admin') != '1':
        return "Không có quyền", 403
    anns = load_announcements()
    if 0 <= index < len(anns):
        anns[index]['is_scrolling'] = not anns[index].get('is_scrolling', False)
        save_announcements(anns)
    return redirect(url_for('announcements', admin=1))

@app.route('/toggle_popup/<int:index>', methods=['POST'])
def toggle_popup(index):
    if request.args.get('admin') != '1':
        return "Không có quyền", 403
    anns = load_announcements()
    if 0 <= index < len(anns):
        anns[index]['is_popup'] = not anns[index].get('is_popup', False)
        save_announcements(anns)
    return redirect(url_for('announcements', admin=1))

# ================== LINEUP ==================
@app.route('/get_lineup/<schedule_id>')
def get_lineup(schedule_id):
    with open('db.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    lineup = data.get('lineups', {}).get(schedule_id, {})
    return jsonify(lineup)

@app.route('/save_lineup/<schedule_id>', methods=['POST'])
def save_lineup(schedule_id):
    lineup_data = request.json
    with open('db.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    if 'lineups' not in data:
        data['lineups'] = {}
    data['lineups'][schedule_id] = lineup_data
    with open('db.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return jsonify({'status': 'ok'})
# ================== BACKUP TO GITHUB ==================
GITHUB_REPO = "Thanh-thuc/football-app"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # sẽ set env trên Render

def backup_to_github():
    try:
        with open(DB_FILE, "rb") as f:
            content = f.read()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"backup/db_{timestamp}.json"

        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{backup_filename}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        data = {
            "message": f"Backup db.json at {timestamp}",
            "content": base64.b64encode(content).decode("utf-8")
        }

        response = requests.put(url, headers=headers, json=data)
        print("Backup response:", response.json())

    except Exception as e:
        print("Backup failed:", e)


def start_scheduler():
    scheduler = BackgroundScheduler()
    # backup mỗi ngày lúc 2h sáng
    scheduler.add_job(func=backup_to_github, trigger="cron", hour=2, minute=0)
    scheduler.start()

    import atexit
    atexit.register(lambda: scheduler.shutdown(wait=False))
start_scheduler()
if __name__ == "__main__":
    # khởi động scheduler khi Flask app chạy
    app.run(debug=True)
