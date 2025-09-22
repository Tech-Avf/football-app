from flask import Flask, render_template, request, redirect, url_for, jsonify
import json, os, time, base64, requests, threading
from datetime import datetime
import pytz
from apscheduler.schedulers.background import BackgroundScheduler

def load_data_from_drive():
    """
    Luôn tải dữ liệu trực tiếp từ Drive, không đọc local.
    """
    try:
        data = download_db()
        if not isinstance(data, dict):
            raise ValueError("Drive data invalid")
        # Đảm bảo có các key mặc định
        data.setdefault("schedules", [])
        data.setdefault("players", [])
        data.setdefault("lineups", {})
        return data
    except Exception as e:
        print("[LOAD DRIVE] Failed:", e)
        return {"schedules": [], "players": [], "lineups": {}}


def init_db_from_drive():
    global _data_cache
    print("[INIT] Downloading db.json from Google Drive...")
    try:
        _data_cache = load_data_from_drive()
        print("[INIT] db.json loaded from Drive into cache.")
    except Exception as e:
        print("[INIT] Failed to load db.json from Drive, using empty structure:", e)
        _data_cache = {"schedules": [], "players": [], "lineups": {}}

# --- Google Drive utils ---
from gdrive_utils import download_db, upload_db

app = Flask(__name__)

def is_schedule_locked(schedule, admin_mode=False):
    if admin_mode:
        return False
    now_ts = time.time()
    if schedule.get("manual_locked"):
        return True
    if schedule.get("locked_at") and now_ts > schedule["locked_at"]:
        return True
    return False

# ================== CONFIG ==================
LOCK_DURATION = 60  # giây, global duy nhất
DB_FILE = "db.json"
ANNOUNCE_FILE = "data/announcements.json"

# ================== ANNOUNCEMENTS UTILS ==================
def load_announcements():
    if os.path.exists(ANNOUNCE_FILE):
        with open(ANNOUNCE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_announcements(data):
    os.makedirs(os.path.dirname(ANNOUNCE_FILE), exist_ok=True)
    with open(ANNOUNCE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ================== LOCAL DB FALLBACK ==================
if not os.path.exists(DB_FILE):
    initial_data = {"schedules": [], "players": []}
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(initial_data, f, ensure_ascii=False, indent=2)

# ================== DB LAYER ==================
_data_cache = None

def load_data(force_refresh=False, use_drive=False):
    """
    force_refresh: bỏ cache hiện tại
    use_drive: nếu True, luôn tải dữ liệu trực tiếp từ Drive
    """
    global _data_cache
    if _data_cache is not None and not force_refresh:
        return _data_cache

    if use_drive:
        _data_cache = load_data_from_drive()
        return _data_cache

    # fallback local (vẫn giữ để test)
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("schedules", [])
            data.setdefault("players", [])
            data.setdefault("lineups", {})
            _data_cache = data
            return data
    except Exception as e:
        print("load_data failed:", e)
        _data_cache = {"schedules": [], "players": [], "lineups": {}}
        return _data_cache


def save_data(data):
    # 1) Save local
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("save_data: Save local failed:", e)

    # 2) Backup tạm
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"db_backups/db_{timestamp}.json"
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    try:
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("save_data: temp backup failed:", e)

    # 3) Upload Drive async
    def _upload():
        try:
            upload_db(data)
            app.logger.info(f"[UPLOAD] db.json uploaded SUCCESS")
        except Exception as e:
            app.logger.error(f"[UPLOAD] db.json failed: {e}")

    threading.Thread(target=_upload).start()


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
    result = []
    for p in data["players"]:
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

# ================== TEMPLATE HELPERS ==================
def _format_date_with_weekday(date_str: str) -> str:
    """Định dạng YYYY-MM-DD -> Thứ ..., dd/mm/YYYY"""
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekday_vi = ["Thứ Hai", "Thứ Ba", "Thứ Tư",
                      "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
        return f"{weekday_vi[dt.weekday()]} | {dt.strftime('%d/%m/%Y')}"
    except Exception:
        return date_str

app.jinja_env.filters["format_date_with_weekday"] = _format_date_with_weekday

@app.context_processor
def inject_helpers():
    def get_line_color(pos):
        if pos.startswith("CB") or pos in ["LB", "RB"]:
            return "DEF"
        elif pos in ["GK"]:
            return "GK"
        elif pos in ["CM", "LM", "RM", "DM", "AM"]:
            return "MID"
        elif pos in ["ST", "CF", "SS", "LW", "RW"]:
            return "FWD"
        return ""
    return dict(
        get_line_color=get_line_color,
        format_date_with_weekday=_format_date_with_weekday
    )

# ================== ROUTES ==================

# INDEX
@app.route("/")
def index():
    data = load_data(use_drive=True)  # luôn lấy Drive
    admin_mode = request.args.get("admin") == "1"

    # Sắp xếp theo ngày giảm dần (mới nhất trước)
    sorted_schedules = sorted(
        data.get("schedules", []),
        key=lambda s: datetime.strptime(
            f"{s.get('date','1970-01-01')} {s.get('time','00:00')}", "%Y-%m-%d %H:%M"
        ),
        reverse=True
    )

    # 🔹 Lọc trận sắp tới (chưa có kết quả) và trận đã đấu (có kết quả)
    upcoming = [s for s in sorted_schedules if not s.get("result")]
    past = [s for s in sorted_schedules if s.get("result")]

    newest_upcoming = upcoming[0]["id"] if upcoming else None

    # Announcements
    anns = load_announcements()
    for a in anns:
        if a.get("is_banner") and "is_scrolling" not in a:
            a["is_scrolling"] = True

    banner_announcement = next((a for a in anns if a.get("is_scrolling")), None)
    popup_announcement = next((a for a in anns if a.get("is_popup")), None)

    return render_template(
        "index.html",
        upcoming=upcoming,
        past=past,
        admin=admin_mode,
        newest_upcoming=newest_upcoming,
        banner_announcement=banner_announcement,
        popup_announcement=popup_announcement
    )

# ================== REGISTER ==================
@app.route("/register/<date>", methods=["GET", "POST"])
def register(date):
    data = load_data()
    schedule = next((s for s in data["schedules"] if s["id"] == date), None)
    if not schedule:
        return f"Không tìm thấy ngày {date}", 404

    players = sorted(data.get("players", []), key=lambda p: p.get("order", 0))
    statuses = schedule.setdefault("status", {})  # ensure dict
    prev_states = {pid: st.get("state", "") for pid, st in statuses.items()}
    admin_mode = request.args.get("admin") == "1"
    now = time.time()


    if request.method == "POST":
        for player in players:
            pid = str(player["id"])
            current = statuses.get(pid, {})

            # Lấy state, note, reason từ form
            state = request.form.get(f"state_{pid}", current.get("state", ""))
            note = request.form.get(f"note_{pid}", current.get("note", ""))
            reason = request.form.get(f"reason_{pid}", current.get("reason", ""))

            lock_time = current.get("locked_at", 0)
            is_player_locked = lock_time and (now - lock_time < LOCK_DURATION)

            if not admin_mode and is_player_locked:
                continue  # skip nếu đang lock

            # Cập nhật lock timestamp cho user
            if not admin_mode:
                if state in ["join", "busy"]:
                    lock_time = now
                else:
                    lock_time = 0

            # Cập nhật status
            statuses[pid] = {
                "state": state,
                "note": note,
                "reason": reason,
                "locked_at": lock_time
            }

        # Save data
        schedule["status"] = statuses
        save_data(data)

        # Ghi log
        state_map = {"join": "tham gia", "busy": "bận"}
        for pid, st in statuses.items():
            new_state = st.get("state")
            if new_state in ["join", "busy"] and new_state != prev_states.get(pid):
                player = next((p for p in players if str(p["id"]) == pid), None)
                name = player["name"] if player else f"ID {pid}"
                app.logger.info(f"[REGISTER] {date} | {name} -> {state_map.get(new_state, new_state)}")

        return redirect(url_for("register", date=date) + ("?admin=1" if admin_mode else ""))

   # Tính locked global đúng thời gian
    locked_at_ts = schedule.get("locked_at", 0)
    manual_locked = schedule.get("manual_locked", False)
    is_locked = is_schedule_locked(schedule, admin_mode)

    locked_at_global = schedule.get("locked_at", 0)
    # Cập nhật trạng thái lock cho tất cả player
    now_ts = time.time()
    for pid, st in schedule.get("status", {}).items():
        st['is_locked'] = not admin_mode and (st.get('locked_at',0) and now_ts - st.get('locked_at',0) < LOCK_DURATION)

    return render_template(
        "register.html",
        players=players,
        schedule=schedule,
        statuses=statuses,
        lock_duration=LOCK_DURATION,
        admin=admin_mode,
        now=now,
        is_locked=is_locked,
        locked_at_global=locked_at_global
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
    if pid in schedule.get("status", {}):
        del schedule["status"][pid]

    save_data(data)
    return redirect(url_for("register", date=date) + "?admin=1")

@app.route("/register/<date>/add/<int:player_id>", methods=["POST"])
def add_player_to_session(date, player_id):
    admin_mode = request.args.get("admin") == "1"
    if not admin_mode:
        return "Không có quyền thêm", 403

    data = load_data()
    player = next((p for p in data.get("players", []) if p["id"] == player_id), None)
    if not player:
        return f"Không tìm thấy cầu thủ", 404

    schedule = next((s for s in data["schedules"] if s["id"] == date), None)
    if not schedule:
        return f"Không tìm thấy buổi đá", 404

    pid = str(player_id)
    if pid not in schedule.get("status", {}):
        schedule.setdefault("status", {})[pid] = {
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
            } for player in data.get("players", [])
        }

        data["schedules"].append({
            "id": date,
            "date": date,
            "time": time_,
            "field": field,
            "map": map_link,
            "locked_at": locked_at,
            "status": status,
            "result": None
        })

        save_data(data)
        return redirect(url_for("index"))

    return render_template("create.html")

# ================== ADMIN PLAYERS ==================
@app.route("/admin/players", methods=["GET", "POST"])
def admin_players():
    data = load_data()
    players = data.get("players", [])
    admin_mode = True  # admin luôn được truy cập

    now = time.time()

    if request.method == "POST":
        for player in players:
            pid = str(player["id"])
            current = player.get("status", {})  # trạng thái mặc định (nếu cần)

            if request.method == "POST":
                for player in players:
                    pid = str(player["id"])
                    # Lấy trạng thái từ form
                    state = request.form.get(f"state_{pid}", "")
                    note = request.form.get(f"note_{pid}", "")
                    reason = request.form.get(f"reason_{pid}", "")

                    # Admin luôn được cập nhật, không cần lock
                    player["status"] = {
                        "state": state,
                        "note": note,
                        "reason": reason,
                        "locked_at": 0
                    }

                save_data(data)
                return redirect(url_for("admin_players"))


        # Save data
        data["players"] = players
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
    data["schedules"] = [s for s in data.get("schedules", []) if s["id"] != date]
    save_data(data)
    return redirect(url_for("index") + "?admin=1")

# ================== ADMIN LOCK / UNLOCK (manual) ==================
@app.route("/admin/lock/<date>", methods=["POST"])
def admin_lock(date):
    # chỉ admin mới được phép (app đang dùng ?admin=1 làm flag admin)
    if request.args.get("admin") != "1":
        return "Không có quyền", 403

    data = load_data()
    schedule = next((s for s in data.get("schedules", []) if s["id"] == date), None)
    if not schedule:
        return f"Không tìm thấy buổi đá {date}", 404

    schedule["manual_locked"] = True
    save_data(data)

    app.logger.info(f"[LOCK] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {date} locked by admin")
    return redirect(url_for("register", date=date) + "?admin=1")


@app.route("/admin/unlock/<date>", methods=["POST"])
def admin_unlock(date):
    if request.args.get("admin") != "1":
        return "Không có quyền", 403

    data = load_data()
    schedule = next((s for s in data.get("schedules", []) if s["id"] == date), None)
    if not schedule:
        return f"Không tìm thấy buổi đá {date}", 404

    # nếu admin gửi extend_minutes thì gia hạn locked_at về now + extend_minutes
    extend_minutes = request.form.get("extend_minutes")
    if extend_minutes:
        try:
            extend = int(extend_minutes)
            schedule["locked_at"] = int(time.time() + extend * 60)
        except Exception:
            # ignore nếu không hợp lệ
            pass

    schedule["manual_locked"] = False
    save_data(data)

    app.logger.info(f"[UNLOCK] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {date} unlocked by admin (extend_minutes={extend_minutes})")
    return redirect(url_for("register", date=date) + "?admin=1")

# ================== COACH MODE ==================
@app.route("/coach/<date>")
def coach_mode(date):
    schedule = get_schedule(date)
    if not schedule:
        return "Không tìm thấy buổi đá", 404

    data = load_data()
    players = data.get("players", [])
    statuses = schedule.get("status", {})

    joined_players = [
        p for p in players
        if str(p["id"]) in statuses and statuses[str(p["id"])].get("state") == "join"
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

# ================== ANNOUNCEMENTS ==================
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
        "is_scrolling": ('is_scrolling' in request.form) or ('is_banner' in request.form),
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
    data = load_data()
    lineup = data.get('lineups', {}).get(schedule_id, {})
    return jsonify(lineup)

@app.route('/save_lineup/<schedule_id>', methods=['POST'])
def save_lineup(schedule_id):
    lineup_data = request.json
    data = load_data()
    if 'lineups' not in data:
        data['lineups'] = {}
    data['lineups'][schedule_id] = lineup_data
    save_data(data)
    return jsonify({'status': 'ok'})

# ================== BACKUP TO GITHUB ==================
def backup_to_github():
    try:
        data = load_data()
        content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"backup/db_{timestamp}.json"

        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{backup_filename}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        payload = {
            "message": f"Backup db.json at {timestamp}",
            "content": base64.b64encode(content).decode("utf-8")
        }

        response = requests.put(url, headers=headers, json=payload)
        print("Backup response:", response.json())
    except Exception as e:
        print("Backup failed:", e)

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=backup_to_github, trigger="cron", hour=2, minute=0)
    scheduler.start()

    import atexit
    atexit.register(lambda: scheduler.shutdown(wait=False))

start_scheduler()

@app.route("/ping")
def ping():
    return "OK", 200

def is_admin():
    return request.args.get("admin") == "1"

@app.route("/update_result/<date>", methods=["POST"])
def update_result(date):
    if not is_admin():
        return "Unauthorized", 403

    data = load_data()
    for s in data.get("schedules", []):
        if s["id"] == date:
            # Lấy tỷ số từ form
            home_score = request.form.get("home_score")
            away_score = request.form.get("away_score")

            if home_score is not None and away_score is not None:
                try:
                    home_score = int(home_score)
                    away_score = int(away_score)
                except ValueError:
                    home_score, away_score = None, None

                if home_score is not None and away_score is not None:
                    s["result"] = {
                        "home": "VF UNITED",
                        "away": "Opponent",
                        "home_score": home_score,
                        "away_score": away_score,
                        "updated_at": datetime.now().isoformat()
                    }
            break

    save_data(data)
    return redirect(url_for("index", admin=1))

@app.route("/seasons")
def seasons():
    data = load_data()
    schedules = data.get("schedules", [])

    seasons_stats = {}

    for s in schedules:
        if not s.get("result"):
            continue

        # Lấy năm từ ngày
        year = s["date"][:4]
        res = s["result"]
        hs, as_ = res.get("home_score", 0), res.get("away_score", 0)
        diff = hs - as_

        if year not in seasons_stats:
            seasons_stats[year] = {
                "matches": 0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "goals_for": 0,
                "goals_against": 0,
                "biggest_win": None,
                "biggest_loss": None
            }

        stats = seasons_stats[year]
        stats["matches"] += 1
        stats["goals_for"] += hs
        stats["goals_against"] += as_

        if hs > as_:
            stats["wins"] += 1
            if stats["biggest_win"] is None or diff > stats["biggest_win"]["diff"]:
                stats["biggest_win"] = {"score": f"{hs}-{as_}", "date": s["date"], "diff": diff}
        elif hs < as_:
            stats["losses"] += 1
            if stats["biggest_loss"] is None or diff < stats["biggest_loss"]["diff"]:
                stats["biggest_loss"] = {"score": f"{hs}-{as_}", "date": s["date"], "diff": diff}
        else:
            stats["draws"] += 1

    # Sắp xếp theo năm giảm dần để thấy năm gần nhất đầu tiên
    seasons_stats = dict(sorted(seasons_stats.items(), key=lambda x: x[0], reverse=True))

    return render_template("seasons.html", seasons=seasons_stats)

if __name__ == "__main__":
    init_db_from_drive()
    app.run(host="0.0.0.0", port=5000)
