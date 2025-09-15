from gdrive_utils import download_db, upload_db

# Test tải file db.json từ Google Drive
print("🔽 Đang tải db.json từ Google Drive...")
data = download_db()
print("Nội dung db.json hiện tại:", data)

# Test upload lại (giữ nguyên hoặc sửa thử)
print("🔼 Đang ghi db.json lên Google Drive...")
data["test_key"] = "hello world"  # thêm key test
upload_db(data)

print("✅ Hoàn thành! Kiểm tra lại db.json trên Google Drive.")
