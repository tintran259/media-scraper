# Triển khai trên Oracle Cloud Always Free (miễn phí)

Dùng VM ARM "Always Free" của Oracle (chạy được Docker + Chromium). Làm theo đúng thứ tự.
Hai chỗ hay sai nhất: **(B) chọn đúng máy ARM** và **(D) mở cổng ở CẢ 2 lớp**.

---

## A. Tạo tài khoản
- Đăng ký tại https://www.oracle.com/cloud/free/ (cần thẻ để **xác minh**, Always Free **không bị trừ tiền**).
- Chọn Home Region gần bạn (vd Singapore). Region không đổi được sau này.

## B. Tạo máy ảo (VM) — chọn ĐÚNG máy ARM
Menu ☰ → Compute → Instances → **Create instance**:
- **Image**: Canonical **Ubuntu 22.04** (bản **aarch64/ARM**).
- **Shape**: bấm *Change shape* → **Ampere** → **VM.Standard.A1.Flex** → chọn **2 OCPU / 12 GB RAM**
  (Always Free cho tổng tối đa 4 OCPU + 24 GB; 2/12 là dư cho tool này).
  ⚠️ **ĐỪNG** chọn `VM.Standard.E2.1.Micro` (AMD, chỉ 1 GB RAM — quá nhỏ cho trình duyệt).
- **SSH keys**: chọn *Generate a key pair* → **tải private key** về máy (giữ kỹ). Hoặc dán public key của bạn.
- Create. Chờ máy chạy, ghi lại **Public IP address**.

> Nếu báo **"Out of host capacity"** (rất hay gặp với ARM): đổi **Availability Domain** (AD-1/2/3) rồi thử lại,
> hoặc thử lại sau vài giờ. Nâng tài khoản lên *Pay As You Go* (vẫn free trong hạn) giúp dễ xin máy ARM hơn.

## C. Trỏ domain
Ở nơi quản lý domain: tạo **bản ghi A** trỏ domain/subdomain về **Public IP** của VM.
Vd: `cao-binh-luan.tenmien.com  →  A  →  140.xxx.xxx.xxx`.

## D. Mở cổng 80/443 — PHẢI làm ở CẢ 2 lớp
Oracle chặn mạng ở 2 nơi, thiếu một là không vào được web.

**D1. Lớp cloud (Security List):**
Networking → Virtual Cloud Networks → (VCN của bạn) → Subnet → **Security List** → **Add Ingress Rules**:
- Source `0.0.0.0/0`, IP Protocol **TCP**, Destination Port **80**
- Source `0.0.0.0/0`, IP Protocol **TCP**, Destination Port **443**

**D2. Lớp hệ điều hành (iptables):**
SSH vào máy rồi chạy (ảnh Ubuntu của Oracle chặn sẵn mọi cổng trừ 22):
```bash
ssh -i duong-dan-private-key ubuntu@PUBLIC_IP

sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```
(Nếu thiếu lệnh netfilter-persistent: `sudo apt update && sudo apt install -y iptables-persistent`, chọn Yes để lưu.)

## E. Cài Docker
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
exit        # thoát SSH rồi ssh vào lại để quyền docker có hiệu lực
```

## F. Đưa code lên & chạy
Đưa thư mục `fb-comment-scraper` lên máy. Cách gọn nhất là cài git rồi clone từ repo của bạn; hoặc từ **máy của bạn**:
```bash
scp -i private-key -r fb-comment-scraper ubuntu@PUBLIC_IP:/home/ubuntu/
```
Trên VM:
```bash
cd ~/fb-comment-scraper
cp .env.example .env
nano .env          # sửa DOMAIN (đã trỏ ở bước C) và ADMIN_TOKEN (chuỗi dài ngẫu nhiên)
docker compose up -d --build
docker compose logs -f app     # xem log, Ctrl+C để thoát xem
```
Lần đầu build ~vài phút (tải image Playwright). Caddy tự xin HTTPS cho domain.

Xong: mở **https://DOMAIN** (trang người dùng), **https://DOMAIN/admin** (nhập ADMIN_TOKEN).

---

## Ghi chú cho máy ARM
- Image Docker `mcr.microsoft.com/playwright/python` có sẵn bản **arm64**, nên chạy trên máy ARM của Oracle bình thường.
- Nếu build báo lỗi liên quan kiến trúc/tag, kiểm tra `python -m playwright --version` và đổi tag trong `Dockerfile` cho khớp (xem DEPLOY.md mục 8).

## Vận hành
- Tự chạy lại khi reboot: đã bật `restart: always`.
- Cập nhật: sửa file rồi `docker compose up -d --build`.
- Sao lưu: thư mục `~/fb-comment-scraper/data` (chứa job + cookie đã mã hoá + khóa). Backup thư mục này.
- Đăng nhập nền tảng: trên server không có màn hình → người dùng **dán cookie**, hoặc admin **tải file phiên** lên `/admin` (xem DEPLOY.md mục 5).

## Bảo mật
- Chỉ mở 22/80/443. `ADMIN_TOKEN` đặt mạnh.
- `data/` chứa cookie + khóa mã hoá — đừng để lộ, đừng commit lên git công khai.
