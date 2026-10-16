# Chạy online bằng Cloudflare Tunnel (miễn phí, không cần domain/thẻ)

Chạy app ngay trên máy bạn, rồi Cloudflare tạo một **link HTTPS công khai** để người khác dùng.

## Cách dùng (1 bước)
Bấm đúp **`start-online.bat`**. Nó sẽ:
1. Cài thư viện còn thiếu (lần đầu).
2. Tải `cloudflared.exe` (lần đầu).
3. Mở app ở một cửa sổ (xem log + **mật khẩu admin** ở đó).
4. Tạo tunnel và in ra **link chia sẻ** dạng `https://xxxx-yyyy.trycloudflare.com`.

Gửi link đó cho người dùng. Trang quản trị = link + `/admin` (nhập mật khẩu admin thấy ở cửa sổ app).

## Lưu ý quan trọng
- **Giữ cả 2 cửa sổ mở** (app + tunnel). Đóng cửa sổ nào là web tắt.
- **Máy phải bật 24/7** thì web mới luôn sống.
- **Tắt/mở lại sẽ ĐỔI link.** Muốn giữ link cùng một địa chỉ thì để nguyên 2 cửa sổ chạy liên tục. (Muốn link **cố định, đẹp** thì cần mua domain rẻ — xem DEPLOY.md.)
- Quick Tunnel hợp cho **nhóm nhỏ / dùng thử**. Cần ổn định lâu dài, nhiều người → nên dùng domain + named tunnel, hoặc VPS.

## Đăng nhập nền tảng
- Người dùng tự vào tab (Facebook/Threads/Instagram) → dán cookie của họ.
- Hoặc admin dùng tài khoản chung: bấm đúp `dang-nhap-facebook.bat` tạo phiên, rồi nạp ở `/admin`.

## Dừng
Đóng 2 cửa sổ (app + tunnel), hoặc Ctrl+C trong từng cửa sổ.

## Nếu gặp lỗi
- `cloudflared.exe` không tải được: tải tay từ https://github.com/cloudflare/cloudflared/releases (file `cloudflared-windows-amd64.exe`), đổi tên thành `cloudflared.exe`, để cùng thư mục, rồi chạy lại.
- Link mở ra nhưng trắng/timeout: xem cửa sổ app có đang chạy ở `http://127.0.0.1:8765` không; chờ app khởi động xong rồi tunnel mới nối được.
