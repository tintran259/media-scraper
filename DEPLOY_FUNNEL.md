# Link CỐ ĐỊNH 24/7 miễn phí, KHÔNG cần domain (Tailscale Funnel)

Tailscale Funnel cho bạn một **link HTTPS cố định** dạng `https://<ten-may>.<ten-tailnet>.ts.net`.
Link này **không bao giờ đổi** dù rớt mạng hay khởi động lại máy — khác hẳn Quick Tunnel của Cloudflare.
Hoàn toàn **miễn phí** cho cá nhân, **không cần mua domain**, và **không có trang cảnh báo** chặn người dùng.

---

## Cài 1 lần (khoảng 5 phút)

### B1. Cài Tailscale
- Tải bản Windows: https://tailscale.com/download/windows
- Cài xong, mở Tailscale → **Log in** (đăng nhập bằng Google/Microsoft/email đều được).
  Đây là tài khoản của BẠN (người vận hành), người dùng cuối KHÔNG cần cài gì.

### B2. Bật Funnel cho tài khoản (1 lần)
Lần đầu chạy, nếu thấy báo **"Funnel is not enabled"** kèm một đường link:
- Mở link đó trên trình duyệt → bấm bật (Enable) Funnel cho máy này.
- Hoặc vào https://login.tailscale.com/admin/dns bật **HTTPS Certificates**, và trong
  **Access controls** đảm bảo thuộc tính `funnel` được cấp (mặc định thường đã bật).
- Bật xong, chạy lại `start-online-funnel.bat`.

### B3. Chạy
Bấm đúp **`start-online-funnel.bat`**. Nó sẽ:
1. Chặn máy ngủ (giữ web sống 24/7).
2. Kiểm tra/đăng nhập Tailscale.
3. Mở app ở một cửa sổ (xem **mật khẩu admin** ở đó).
4. In ra **LINK CỐ ĐỊNH** dạng `https://....ts.net`.

---

## Dùng
- **Gửi link `....ts.net`** cho người dùng — họ mở là vào, có HTTPS, không bị trang cảnh báo.
- **Trang quản trị**: link đó + `/admin` (nhập mật khẩu admin thấy ở cửa sổ app).
- Người dùng vào tab (Facebook/Threads/Instagram) → dán cookie của họ để cào.

## Vì sao ổn định hơn Quick Tunnel
- Link **cố định** — không đổi khi rớt/khởi động lại.
- Funnel tự nối lại; file `.bat` cũng có vòng lặp chạy lại nếu lỡ dừng.
- Không có trang "Visit Site" chắn ngang như ngrok.

## Lưu ý
- **Vẫn phải giữ máy bật** (web chạy trên máy bạn). File `.bat` đã tự tắt chế độ ngủ;
  nên vào *Settings → System → Power → Sleep = Never* cho chắc.
- **Giữ cả 2 cửa sổ mở** (app + funnel). Đóng là web tắt.
- Chỉ mở đúng cổng web của tool (8765). Đừng funnel các dịch vụ nhạy cảm khác —
  ai có link `.ts.net` đều vào được, nên để `ADMIN_TOKEN` mạnh.
- Nếu sau khi cập nhật Tailscale mà Funnel lỗi handshake, đó là bug theo phiên bản —
  cập nhật lên bản mới hơn hoặc tạm lùi 1 bản.

## Muốn link NGẮN GỌN, ĐẸP hơn
Link `.ts.net` hơi dài. Muốn dạng `https://cao-binh-luan.tenban.com` thì cần mua 1 domain
rẻ rồi dùng Cloudflare named tunnel — xem mục "Tunnel cố định" trong `DEPLOY_CLOUDFLARE.md`.
