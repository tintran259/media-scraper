# Triển khai lên server (chạy 24/7, ra domain)

Tool chạy trình duyệt ẩn (Chromium) nên **không dùng được** hosting tĩnh/serverless (Vercel, Netlify, GitHub Pages…).
Cần một **VPS** (máy chủ ảo) chạy Docker. Hướng dẫn dưới dùng Docker + Caddy (tự cấp HTTPS).

---

## 1. Chuẩn bị

**VPS**: Ubuntu 22.04/24.04, tối thiểu **2 GB RAM** (trình duyệt ẩn khá tốn RAM; 4 GB cho thoải mái), 1–2 vCPU.
Nhà cung cấp: DigitalOcean, Vultr, Linode, Hetzner, hoặc VPS trong nước.

**Domain**: trỏ **bản ghi A** của domain (hoặc subdomain) về **IP của VPS**.
Ví dụ: `cao-binh-luan.tenmien.com  A  123.45.67.89`. Chờ DNS cập nhật (vài phút–vài giờ).

**Cài Docker trên VPS** (một lần):
```bash
curl -fsSL https://get.docker.com | sh
```

---

## 2. Đưa code lên VPS

Copy cả thư mục `fb-comment-scraper` lên VPS (qua `git clone`, `scp`, hoặc rsync). Ví dụ:
```bash
scp -r fb-comment-scraper user@IP:/home/user/
```

Trên VPS, vào thư mục đó:
```bash
cd fb-comment-scraper
```

---

## 3. Cấu hình

```bash
cp .env.example .env
nano .env        # sửa DOMAIN và ADMIN_TOKEN
```
- `DOMAIN`: domain đã trỏ về VPS (vd `cao-binh-luan.tenmien.com`).
- `ADMIN_TOKEN`: đặt **một chuỗi ngẫu nhiên thật dài** (mật khẩu trang quản trị). Sinh nhanh: `openssl rand -base64 24`.

Không cần sửa `Caddyfile` (nó tự đọc DOMAIN từ .env).

---

## 4. Chạy

```bash
docker compose up -d --build
```
Lần đầu build hơi lâu (tải image Playwright ~1–2 GB). Caddy sẽ tự xin chứng chỉ HTTPS cho domain.

Xong, mở: **https://DOMAIN-cua-ban** → trang người dùng. Trang quản trị: **https://DOMAIN/admin** (nhập ADMIN_TOKEN).

Xem log:
```bash
docker compose logs -f app
```

---

## 5. Đăng nhập Facebook/Threads/Instagram trên server

**Quan trọng:** nút “Đăng nhập Facebook” (mở cửa sổ trình duyệt) **không chạy trên server** vì server không có màn hình. Dùng một trong hai cách:

- **Người dùng tự dán cookie** (khuyến nghị): mỗi người vào tab nền tảng → ô “Tài khoản của bạn” → dán cookie (Cookie-Editor export JSON). Cách này chạy bình thường trên server.
- **Tài khoản chung (admin)**: trên **máy của bạn** (có màn hình), bấm đúp `dang-nhap-facebook.bat` để tạo `data/fb_state.json`, rồi vào `/admin` trên server → mục phiên Facebook → **tải file phiên lên**.

Cookie được **mã hoá** khi lưu (khóa ở `data/.session_key`). Dùng **tài khoản phụ**.

---

## 6. Vận hành

- **Tự chạy lại khi reboot/crash**: đã bật `restart: always`, không cần làm gì.
- **Cập nhật code**: cập nhật file rồi `docker compose up -d --build`.
- **Dừng**: `docker compose down`. **Xem trạng thái**: `docker compose ps`.
- **Sao lưu dữ liệu**: toàn bộ nằm ở thư mục `./data` (jobs, cookie đã mã hoá, khóa mã hoá). Backup thư mục này là đủ. **Đừng để lộ `data/`** (chứa cookie + khóa).

---

## 7. Bảo mật & lưu ý

- Chỉ **Caddy** mở cổng 80/443 ra ngoài; app chạy nội bộ (không expose uvicorn trực tiếp).
- Đặt `ADMIN_TOKEN` mạnh. Ai có token mới vào được `/admin` (đổi cấu hình, xem job mọi người, nạp cookie chung).
- Bật tường lửa VPS, chỉ mở 22 (SSH), 80, 443.
- **Rủi ro nền tảng**: Facebook/Threads/Instagram đều của Meta. Cào tự động vi phạm Điều khoản của họ; tài khoản dùng để cào có thể bị khóa. Dùng tài khoản phụ, tốc độ vừa phải. Cân nhắc quy định dữ liệu cá nhân (Nghị định 13/2023/NĐ-CP) nếu lưu/dùng dữ liệu người bình luận.
- Nếu bạn giữ cookie của nhiều người trên server, đó là dữ liệu nhạy cảm — bảo vệ máy chủ cẩn thận.

---

## 8. Nếu build lỗi image

Tag image Playwright phải khớp phiên bản playwright. Kiểm tra:
```bash
python -m playwright --version      # vd: Version 1.64.0
```
Rồi sửa dòng đầu `Dockerfile` cho khớp: `FROM mcr.microsoft.com/playwright/python:v1.64.0-noble`
(xem tag có sẵn tại https://mcr.microsoft.com/en-us/product/playwright/python/tags).

---

## 9. Mở rộng tốc độ/quy mô

- Một bài cào tuần tự theo cursor — không nhanh hơn được (giới hạn nền tảng). Muốn throughput cao: nhiều tài khoản + nhiều bài song song (chỉnh “Số bài chạy cùng lúc” trong `/admin`), và thêm **proxy** (mỗi dòng một proxy ở `/admin`) để phân tán IP, giảm nguy cơ bị khóa.
- RAM là giới hạn chính (mỗi bài đang chạy mở một trình duyệt/ngữ cảnh). Chạy nhiều bài song song thì tăng RAM VPS.
