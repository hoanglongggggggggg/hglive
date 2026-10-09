# HGLIVE — ghi livestream Douyin

Tool canh và ghi luồng live Douyin (`live.douyin.com`) ra file bằng ffmpeg. Có giao
diện PySide6 để canh nhiều phòng cùng lúc, và dòng lệnh dùng chung engine.

- Dán link phòng, tool dò thông tin phòng + các mức nét.
- **Canh sóng**: chủ kênh lên sóng là tự ghi, luồng đứt giữa chừng thì tự nối tiếp.
- Ghi nhiều phòng song song, có hàng chờ khi vượt số luồng cho phép.
- Không cần đăng nhập, không cần ký `a_bogus`: chỉ cần cookie `ttwid`, mà tool tự xin.
- Tuỳ chọn chia **làn mạng** (danh sách proxy hoặc NordVPN WireGuard) để mỗi phòng
  đi một IP, tránh bị CDN bóp băng thông khi ghi nhiều phòng.

## Cài đặt

Cần:

- Python 3.9+
- ffmpeg trong `PATH` (Windows: `winget install Gyan.FFmpeg`)

```bash
pip install -r requirements.txt
```

## Chạy

**Giao diện:** bấm đúp `HGLIVE.bat`, hoặc:

```bash
python hglive.py
```

**Dòng lệnh:**

```bash
python hglive.py "https://live.douyin.com/578280456923" --list    # xem phòng + mức nét, không ghi
python hglive.py 578280456923                                      # ghi tới khi tắt sóng
python hglive.py 578280456923 -q ld -t 600 --ext mp4               # ghi 10 phút, mức ld, ra mp4
python hglive.py 578280456923 --wait                               # canh, lên sóng là ghi
python hglive.py 578280456923 --url-only                           # chỉ in URL luồng (dán vào VLC)
python hglive.py 578280456923 --lanes "http://a:8080,http://b:8080" --no-direct
python hglive.py 578280456923 --lanes nordvpn --lane-country JP
```

`link` nhận được: `live.douyin.com/<rid>`, link rút gọn `v.douyin.com/...`, hoặc
chỉ số `web_rid`.

### Tuỳ chọn dòng lệnh

| Tuỳ chọn | Ý nghĩa |
|---|---|
| `--list` | chỉ liệt kê phòng + mức nét |
| `--url-only` | chỉ in URL luồng đã chọn |
| `-q`, `--quality` | `best` · `worst` · `origin` / `uhd` / `hd` / `sd` / `ld` / `md` · `ao` (chỉ tiếng) |
| `--proto` | `flv` (mặc định, trễ thấp) · `hls` (chịu mạng chập hơn) |
| `--ext` | `ts` (khuyên dùng, mất điện vẫn phát được) · `mp4` · `flv` |
| `-t`, `--time` | ghi bao nhiêu giây (mặc định: tới khi tắt sóng) |
| `--segment N` | cắt file mỗi N phút |
| `--out DIR` | thư mục ra |
| `--cookie FILE` | file cookie (Netscape `cookies.txt` hoặc JSON export) — thường không cần |
| `--proxy URL` | một proxy cho tất cả |
| `--lanes MODE` | `off` · `nordvpn` · danh sách proxy ngăn bởi dấu phẩy · file `.txt` mỗi dòng một proxy |
| `--lane-country` | nước cho làn NordVPN, vd `JP` |
| `--no-direct` | không dùng IP máy làm một làn |
| `--wait` | canh tới khi phòng lên sóng |
| `--every N` | chu kỳ dò lại khi canh (giây, tối thiểu 15) |
| `--no-resume` | luồng đứt là dừng, không ghi tiếp |
| `--no-meta` | không lưu `.info.json` / ảnh bìa |
| `-v` | in cả dòng ffmpeg |

## Dùng giao diện

1. Dán link vào ô trên cùng → **Dò phòng**.
2. **Ghi ngay** nếu phòng đang phát, hoặc **Canh sóng** để chờ lên sóng rồi tự ghi.
3. Mỗi dòng có nút riêng:
   - **Tạm dừng** (`Space`): dừng ghi, đóng file tử tế, *vẫn canh sóng*.
   - **Ghi tiếp**: ghi vào file mới, cột thời lượng/dung lượng cộng dồn.
   - **Dừng hẳn**: dừng và thôi canh.
4. Ba nút ở chân trang áp cho các dòng đang chọn; không chọn dòng nào thì áp cho tất cả.
5. **Cài đặt**: thư mục lưu, mức nét, giao thức, định dạng, cắt file, số phòng ghi
   cùng lúc, mẫu tên file, làn mạng.
6. **Làn mạng**: xem làn nào đang chở phòng nào, xoay IP làn đang rảnh.

Phòng hiện **`xếp hàng`** (vàng) nghĩa là đang chờ suất ghi vì đã đủ
`Ghi cùng lúc tối đa`, không phải bị treo.

Mở lại app sẽ canh lại đúng danh sách phòng lần trước.

## Cấu hình

Lưu ở `hglive.config.json` cạnh tool (tự tạo lần đầu, đã nằm trong `.gitignore`).
Xem mẫu ở [`hglive.config.example.json`](hglive.config.example.json).

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `out_dir` | `recordings/` cạnh tool | nơi lưu file |
| `quality` / `proto` / `ext` | `best` / `flv` / `ts` | như tuỳ chọn dòng lệnh |
| `auto_record` | `true` | thấy lên sóng là ghi ngay |
| `auto_resume` | `true` | đứt giữa buổi mà phòng còn phát thì nối tiếp |
| `poll_every` | `45` | chu kỳ dò khi chờ sóng (giây; dò dày quá dễ dính risk-control) |
| `segment_min` | `0` | cắt file mỗi N phút (0 = một file liền) |
| `max_hours` | `0` | trần thời lượng mỗi lần ghi (0 = không giới hạn) |
| `max_parallel` | `3` | số phòng được ghi cùng lúc |
| `name_tpl` | `{nick}_{date}_{time}_{quality}` | mẫu tên file; khoá dùng được: `{nick}` `{rid}` `{room}` `{title}` `{date}` `{time}` `{quality}` `{res}` |
| `room_subdir` | `true` | mỗi phòng một thư mục con |
| `save_meta` | `true` | lưu kèm `.info.json` + ảnh bìa |
| `proxy_mode` | `off` | `off` · `list` · `nordvpn` — xem [ghi chú kỹ thuật](docs/TECHNICAL.md#làn-mạng-proxy) |

### NordVPN (tuỳ chọn)

Chế độ `nordvpn` chỉ cần **access token** NordVPN, không cần cài app. Tool tự tải
`wireproxy` nếu chưa có. Token tìm theo thứ tự: ô Token trong Cài đặt → ô File
token → biến môi trường `NORDVPN_TOKEN` → file `.nordvpn_token` hoặc `.env` cạnh
tool. Token không bao giờ được in ra nhật ký.

## Đóng gói .exe

```bash
pip install pyinstaller
build_live.bat
```

Ra `dist\HGLIVE.exe` (onefile, không console). Bản exe lưu config và `recordings/`
cạnh file `.exe`.

## Cấu trúc

```
hglive.py            vỏ chạy từ source
hglive_app.py        điểm vào cho PyInstaller (đặt tên khác để không che gói hglive/)
HGLIVE.bat           bấm đúp để chạy
build_live.bat       đóng gói exe
hglive/
  live.py            dò phòng, bóc danh sách luồng (thuần mạng)
  recorder.py        máy trạng thái: canh sóng → ghi → đứt → nối lại
  lanes.py           làn mạng: chia phòng vào lối ra, xoay IP khi rảnh
  nordvpn.py         dựng proxy WireGuard bằng wireproxy
  gui.py             giao diện PySide6
  cli.py             dòng lệnh
  config.py          đọc/ghi hglive.config.json
  cookies.py         đọc cookie (Netscape / JSON / chuỗi header)
  util.py            tiện ích: tên file, định dạng, tìm ffmpeg
docs/TECHNICAL.md    endpoint, chỗ bóc luồng, các bẫy đã gặp, cơ chế làn mạng
```

## Lỗi hay gặp

| Hiện tượng | Cách xử lý |
|---|---|
| "không tìm thấy ffmpeg" | cài ffmpeg và mở lại terminal để `PATH` cập nhật |
| `status = 4` / phòng không có luồng | chủ kênh đã tắt sóng — dùng **Canh sóng** |
| `服务器打瞌睡了` (10001) | server Douyin chập chờn, tool tự thử lại |
| Ghi nhiều phòng mà mỗi luồng chỉ vài chục KB/s | CDN bóp theo IP — bật làn mạng hoặc hạ mức nét |

Chi tiết kỹ thuật: [docs/TECHNICAL.md](docs/TECHNICAL.md).
