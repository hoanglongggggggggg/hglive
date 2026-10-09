# HGLIVE — ghi chú kỹ thuật

Tài liệu cho người sửa code: endpoint, chỗ bóc luồng, các bẫy đã gặp, cơ chế làn
mạng. Hướng dẫn dùng nằm ở [README](../README.md).

---

## Nó hoạt động thế nào

Một endpoint duy nhất (nhánh webcast, khác hẳn API video thường của Douyin):

```
GET https://live.douyin.com/webcast/room/web/enter/?web_rid=<rid>&aid=6383&app_name=douyin_web&…
```

Kết quả đo trên phòng `578280456923` ngày 2026-08-29, mỗi ô chạy 3 lượt:

| Điều kiện | Kết quả |
|---|---|
| có cookie `ttwid` (bất kỳ, kể cả vừa xin xong) | ✅ 31 KB JSON đầy đủ |
| **không có `ttwid`** | ❌ HTTP **200** nhưng body **RỖNG**, không một dòng báo lỗi |
| có / không `a_bogus` | ✅ như nhau — **webcast không cần ký** |
| có / không `Referer`, đổi User-Agent | ✅ như nhau |
| cookie đăng nhập (`sessionid`) | không cần |

Nên cookie thật ra gần như thừa: chỉ `ttwid` có tác dụng, mà một lần `GET
live.douyin.com/<rid>` là server tự phát cho. Tool tự làm bước đó khi thiếu.

Khác với API video thường (phải ký `a_bogus`), nhánh live **không cần signer**,
nên signer có bị Douyin đổi cũng không ảnh hưởng gì ở đây.

## Luồng nằm ở đâu

`data.data[0].stream_url`, nhưng:

- **Đừng dùng `flv_pull_url`** — nhãn web này thiếu mức (phòng thử chỉ hiện 2/4).
- Chỗ đầy đủ là `live_core_sdk_data.pull_data.stream_data` (một chuỗi JSON lồng),
  cho cả `flv` / `hls` / `lls` (SDP, low-latency — ffmpeg không đọc được).
- `web_stream_url` luôn rỗng. Bỏ qua.
- Thứ hạng nét lấy từ `options.qualities[].level`, KHÔNG lấy theo `vbitrate`:
  phòng thử khai `origin` 338 kbps mà đo thực 593 kbps, còn `ld` tự nhận 1000 kbps.
- URL đã ký hiệu lực **168 giờ (7 ngày)** — ghi xuyên đêm không cần lấy lại.
- CDN kéo trần trụi: không cookie, không Referer.

## Những chỗ dễ sập

| Hiện tượng | Nguyên nhân |
|---|---|
| API trả 200 + body rỗng | thiếu `ttwid` (không phải mất mạng) |
| `status_code=10001 服务器打瞌睡了` | server chập chờn nhất thời — thử lại là qua |
| MP4 ra file 0.2 giây, log "Error muxing a packet" | HLS gói AAC kiểu ADTS, MP4 không chứa được → phải có `-bsf:a aac_adtstoasc` |
| Cột dung lượng đứng ở 0 khi bật cắt file | muxer `segment` khai `total_size=N/A` → phải tự cộng bằng cách stat file |
| MP4 hỏng đuôi khi tắt app | dừng bằng `terminate()` → phải gửi `q` qua stdin cho ffmpeg tự đóng |
| `status = 4` | chủ kênh đã tắt sóng, khi đó không có `stream_url` |

## Làn mạng (proxy)

Ghi nhiều phòng cùng lúc qua **một** IP thì CDN bóp băng thông — mỗi luồng tụt
xuống vài chục KB/s dù đường truyền còn rộng. Chia làn để mỗi phòng đi một lối ra.

Một *làn* là một lối ra mạng, phòng bám **dính** một làn (cả gọi API lẫn kéo luồng
đều qua đúng lối đó, nên phía Douyin thấy một phòng luôn đến từ một IP):

| Chế độ | Làn dựng ra sao |
|---|---|
| `off` | chỉ `direct` — IP của máy |
| `list` | mỗi proxy HTTP trong danh sách thành một làn; kiểm thông ngay lúc bật |
| `nordvpn` | mỗi làn là một tiến trình `wireproxy` riêng ôm một exit WireGuard của NordVPN, mở cổng HTTP cố định ở 127.0.0.1 |

Chế độ `nordvpn` chỉ cần **access token** NordVPN, không cần cài app: tool lấy
NordLynx private key từ token, hỏi danh sách server, thử vài con rồi giữ con có
`ms` thấp nhất.

**Khác cốt tử so với proxy cho API thường:** bên đó xoay IP lúc nào cũng được vì
mỗi request chỉ sống vài giây. Ở đây một lần ghi kéo dài hàng giờ — xoay giữa chừng là đứt luồng.
Nên làn **đang ghi thì không bao giờ bị xoay**; lệnh xoay xếp lại chờ tới lúc rảnh.

Chọn làn cho phòng mới: ưu tiên làn **đang ghi ít nhất** (băng thông mới là thứ
đang thiếu), số phòng bám dính chỉ dùng để phá hoà.

An toàn: wireproxy chỉ bind `127.0.0.1`, không có đường nào đổi thành `0.0.0.0`
(bind 0.0.0.0 là biến máy thành open proxy công cộng). Thư mục tạm chứa
private key bị quét xoá lúc khởi động và lúc tắt.

> Token NordVPN lưu **dạng chữ thường** trong `hglive.config.json` cạnh tool.

### Token NordVPN: tìm ở đâu

Thứ tự tìm:

1. ô **Token NordVPN** trong Cài đặt (lưu chữ thường vào `hglive.config.json`)
2. file bạn chỉ ở ô **File token** (file `.env` dạng `NORDVPN_TOKEN=...` hoặc file chỉ chứa token)
3. biến môi trường `NORDVPN_TOKEN` / `NORD_TOKEN` / `NORDVPN_ACCESS_TOKEN`
4. `<thư mục tool>\.nordvpn_token`
5. `<thư mục tool>\.env`

`wireproxy` tìm theo thứ tự: đường dẫn trong Cài đặt → cạnh tool → `PATH`; không
thấy thì tự tải bản `v1.1.2` từ GitHub (windtf/wireproxy). Nhật ký chỉ in **nguồn**
và bản che (`e9f2…31c4`), không bao giờ in token.

### Server NordVPN: phần lớn không trả lời

Đo ngày 2026-08-29 từ đường VN: thử 4 nước, chỉ **Brazil** bắt tay WireGuard xong;
US / DE / FR im lặng hoàn toàn (`Handshake did not complete` lặp mãi). Không phải
mạng chặn UDP 51820 — chặn thì con nào cũng chết.

Nên `_establish` **bỏ server chết sau ~6 giây** thay vì ngồi đủ 12, và
`Trần độ trễ` mặc định thử tới 10 server. Cứ để nó chạy; con nào sống thì bắt tay
xong trong 2–4 giây.

## Quá tải: đọc bảng cho đúng

`Ghi cùng lúc tối đa` (mặc định 3) là số phòng được chạy ffmpeg đồng thời. Phòng
thứ 4 trở đi hiện **`xếp hàng`** (vàng) — đang chờ suất, không phải treo. Trước
đây chúng hiện "ĐANG GHI" với `00:00 / 0 B`, nhìn y như hỏng.

Quá tải thật thì làm theo thứ tự: bật làn mạng → hạ mức nét (`ld` thay `origin`)
→ tăng `Ghi cùng lúc tối đa` chỉ khi đường truyền còn dư.

## Dừng / ghi tiếp từng phòng

Ba mức, đừng lẫn:

| Thao tác | Ghi | Canh sóng | Số liệu |
|---|---|---|---|
| **Tạm dừng** (nút trên dòng, `Space`) | dừng, file đóng tử tế | **vẫn canh** | giữ, ghi tiếp thì cộng dồn |
| **Ghi tiếp** | ghi vào file MỚI | vẫn canh | cộng tiếp vào tổng cũ |
| **Dừng hẳn** | dừng | thôi canh | ghi tiếp = làm lại từ đầu |

Live không nối được vào giữa file cũ, nên "ghi tiếp" luôn đẻ file mới — nhưng cột
Thời lượng/Dung lượng vẫn cộng dồn cả buổi, đó mới là con số bạn cần nhìn.

Nút nằm ngay trên từng dòng. Ba nút ở chân trang ăn theo **vùng chọn**; không chọn
dòng nào thì áp cho tất cả.

## Bố cục

| File | Việc |
|---|---|
| `live.py` | dò phòng, bóc danh sách luồng — thuần mạng, không trạng thái |
| `recorder.py` | máy trạng thái: canh sóng → ghi → đứt → nối lại; đọc `ffmpeg -progress` |
| `lanes.py` | làn mạng: chia phòng vào lối ra, xoay IP khi làn rảnh |
| `nordvpn.py` | dựng proxy WireGuard bằng wireproxy (API NordVPN, đo ms, trông tiến trình) |
| `gui.py` | giao diện PySide6, canh nhiều phòng cùng lúc |
| `cli.py` | dòng lệnh, dùng đúng engine với GUI |
| `config.py` | `hglive.config.json` cạnh tool |
| `cookies.py`, `util.py` | bản chép để thư mục đứng độc lập |

Cấu hình lưu ở `hglive.config.json`, file ghi mặc định vào `recordings/`.
