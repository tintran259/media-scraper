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
- **Tắt/mở lại sẽ ĐỔI link.** Muốn giữ link cùng một địa chỉ thì để nguyên 2 cửa sổ chạy liên tục.
- Quick Tunnel hợp cho **nhóm nhỏ / dùng thử**. Cần ổn định lâu dài, link cố định → xem phần "Hay bị rớt?" bên dưới.

## Hay bị rớt dù máy mở 24/7 (chỉ khóa màn hình)?
Có 2 nguyên nhân, đã xử lý/giải thích:

**1) Windows ngủ máy hoặc ngủ card mạng khi khóa màn hình.**
`start-online.bat` nay tự chạy `powercfg` tắt chế độ ngủ. Làm thêm 1 lần cho chắc:
- *Settings → System → Power* → **Screen and sleep** → đặt **Sleep = Never** (mục "When plugged in").
- *Device Manager* → mở card mạng (Network adapters) → chuột phải → *Properties* → tab **Power Management** → **bỏ tick** "Allow the computer to turn off this device to save power".
- Nếu dùng Wi‑Fi laptop: cắm điện và để *Power mode = Best performance*.

**2) Quick Tunnel (`trycloudflare.com`) vốn là tunnel TẠM — Cloudflare không cam kết 24/7 nên hay rớt, và mỗi lần nối lại thường ĐỔI link.**
`start-online.bat` nay **tự nối lại** khi rớt (vòng lặp) nên web không chết hẳn — nhưng **link có thể đổi**. Người dùng đang giữ link cũ sẽ phải lấy link mới.

👉 **Muốn link CỐ ĐỊNH + ổn định 24/7 thật sự → dùng "tunnel cố định" (named tunnel).** Nó tự nối lại mà **giữ nguyên địa chỉ**.

## Tunnel cố định (link không đổi) — khuyến nghị nếu cho nhiều người dùng lâu dài
Cần: 1 **tài khoản Cloudflare miễn phí** + 1 **domain đã đưa vào Cloudflare** (domain mua rẻ ~vài chục nghìn–vài $/năm; đây là chi phí duy nhất).
1. Tạo tài khoản tại https://dash.cloudflare.com → thêm domain của bạn (đổi nameserver theo hướng dẫn Cloudflare).
2. Chạy 1 lần để đăng nhập máy với Cloudflare:
   ```
   cloudflared.exe tunnel login
   cloudflared.exe tunnel create cao-binh-luan
   cloudflared.exe tunnel route dns cao-binh-luan cao-binh-luan.tenmien-cua-ban.com
   ```
3. Từ nay chạy app rồi chạy:
   ```
   cloudflared.exe tunnel run --url http://localhost:8765 cao-binh-luan
   ```
   Link cố định sẽ là `https://cao-binh-luan.tenmien-cua-ban.com` — không đổi dù rớt/khởi động lại.

> Không có/không muốn mua domain: cứ dùng Quick Tunnel như trên (chấp nhận link đổi khi rớt), hoặc chuyển hẳn lên VPS/Oracle (xem DEPLOY_ORACLE.md) cho ổn định cao nhất.

## Đăng nhập nền tảng
- Người dùng tự vào tab (Facebook/Threads/Instagram) → dán cookie của họ.
- Hoặc admin dùng tài khoản chung: bấm đúp `dang-nhap-facebook.bat` tạo phiên, rồi nạp ở `/admin`.

## Dừng
Đóng 2 cửa sổ (app + tunnel), hoặc Ctrl+C trong từng cửa sổ.

## Nếu gặp lỗi
- `cloudflared.exe` không tải được: tải tay từ https://github.com/cloudflare/cloudflared/releases (file `cloudflared-windows-amd64.exe`), đổi tên thành `cloudflared.exe`, để cùng thư mục, rồi chạy lại.
- Link mở ra nhưng trắng/timeout: xem cửa sổ app có đang chạy ở `http://127.0.0.1:8765` không; chờ app khởi động xong rồi tunnel mới nối được.
