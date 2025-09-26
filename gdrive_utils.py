# gdrive_utils.py
import io
import os
import datetime
import json
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from google.oauth2 import service_account

# Lấy JSON credential từ biến môi trường
SERVICE_ACCOUNT_INFO = os.getenv("SERVICE_ACCOUNT_JSON")

# Scope để thao tác với Google Drive
SCOPES = ["https://www.googleapis.com/auth/drive"]

# ID folder bạn đã share cho service account
FOLDER_ID = "1tz_cbi6LLu2eCXZI54HtNM3dg-94SuVx"

# Tên file db.json trên Google Drive
FILE_NAME = "db.json"

# Tên file announcements.json trên Google Drive
ANNOUNCE_FILE_NAME = "announcements.json"

def get_drive_service():
    """Tạo service kết nối Google Drive từ JSON trong biến môi trường."""
    if not SERVICE_ACCOUNT_INFO:
        raise ValueError("SERVICE_ACCOUNT_JSON chưa được thiết lập trong Environment Variables.")
    
    # Nếu SERVICE_ACCOUNT_INFO là đường dẫn tới file
    if os.path.isfile(SERVICE_ACCOUNT_INFO):
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_INFO, scopes=SCOPES
        )
    else:
        # nếu vẫn là JSON string
        info = json.loads(SERVICE_ACCOUNT_INFO)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    
    return build("drive", "v3", credentials=creds)


def get_file_id(service):
    """Lấy ID của file db.json trong folder Drive."""
    query = f"'{FOLDER_ID}' in parents and name='{FILE_NAME}' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if not files:
        return None
    return files[0]["id"]

def get_file_id_by_name(service, filename):
    """Lấy ID của file bất kỳ theo tên trong folder Drive."""
    query = f"'{FOLDER_ID}' in parents and name='{filename}' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if not files:
        return None
    return files[0]["id"]

def download_db():
    """Tải db.json từ Google Drive về, trả về dict (rỗng nếu chưa có)."""
    service = get_drive_service()
    file_id = get_file_id(service)
    if not file_id:
        print("[gdrive_utils] db.json chưa có trên Drive, trả về dict rỗng.")
        return {}

    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    try:
        return json.load(fh)
    except Exception as e:
        print("[gdrive_utils] Lỗi parse db.json:", e)
        return {}

def upload_db(data):
    """Ghi dict data -> db.json lên Google Drive, kèm last_update."""
    # 1. Thêm timestamp vào dict
    data['last_update'] = datetime.datetime.utcnow().isoformat()
    
    # 2. Tạo service & lấy file_id
    service = get_drive_service()
    file_id = get_file_id(service)

    # 3. Chuẩn bị file media
    fh = io.BytesIO(json.dumps(data, indent=2).encode("utf-8"))
    media = MediaIoBaseUpload(fh, mimetype="application/json")

    # 4. Upload: update nếu file đã tồn tại, create nếu chưa
    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        file_metadata = {"name": FILE_NAME, "parents": [FOLDER_ID]}
        service.files().create(body=file_metadata, media_body=media).execute()

def download_announcements():
    """Tải announcements.json từ Google Drive, trả về list (rỗng nếu chưa có)."""
    service = get_drive_service()
    file_id = get_file_id_by_name(service, ANNOUNCE_FILE_NAME)
    if not file_id:
        print("[gdrive_utils] announcements.json chưa có trên Drive, trả về list rỗng.")
        return []

    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    try:
        return json.load(fh)
    except Exception as e:
        print("[gdrive_utils] Lỗi parse announcements.json:", e)
        return []


def upload_announcements(data):
    """Ghi list data -> announcements.json lên Google Drive, kèm last_update."""
    payload = {
        "last_update": datetime.datetime.utcnow().isoformat(),
        "announcements": data
    }

    service = get_drive_service()
    file_id = get_file_id_by_name(service, ANNOUNCE_FILE_NAME)

    fh = io.BytesIO(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
    media = MediaIoBaseUpload(fh, mimetype="application/json")

    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        file_metadata = {"name": ANNOUNCE_FILE_NAME, "parents": [FOLDER_ID]}
        service.files().create(body=file_metadata, media_body=media).execute()
