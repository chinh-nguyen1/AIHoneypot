# Lưu trữ MySQL bền vững và migration an toàn

Tài liệu này hướng dẫn cấu hình volume và di chuyển dữ liệu MySQL hiện có mà
không xóa hoặc ghi đè database nguồn. Không chạy `seed_data.sql` trên database
đã có dữ liệu: file này có các lệnh `TRUNCATE`.

## Phân biệt lệnh kiểm tra và lệnh thay đổi

- **Chỉ đọc:** `docker ps`, `docker inspect`, `docker volume ls`,
  `docker volume inspect`, các truy vấn `SELECT`, `SHOW GRANTS` và lệnh kiểm
  tra kết nối dùng `SELECT 1`.
- **Có thay đổi:** sửa `.env`, tạo volume, khởi động/dừng container, tạo dump
  (ghi file backup), restore dump, và các lệnh `CREATE USER`, `ALTER USER`,
  `GRANT`. Các bước này cần operator chủ động xác nhận và lưu backup trước.

## 1. Hai tình huống khác nhau

### Cài đặt mới

Compose khai báo volume `mysql_data` và gắn vào `/var/lib/mysql`. Khi chưa có
volume mang tên này, Compose tạo volume rỗng; MySQL khởi tạo database lần đầu.
Mặc định, tên vật lý là `mysql_data`. Có thể cấu hình tên khác bằng biến
`MYSQL_DATA_VOLUME_NAME`.

Các biến `MYSQL_DATABASE`, `MYSQL_USER` và `MYSQL_PASSWORD` chỉ được MySQL image
áp dụng trong lần khởi tạo data directory rỗng đầu tiên. Chúng không tự tạo,
đổi mật khẩu hoặc cấp quyền cho tài khoản trên một database đã khởi tạo.

Quy trình cài mới không phải quy trình migration database hiện có. Đặc biệt,
không để Compose tạo volume rỗng rồi giả định dữ liệu từ volume cũ đã được
chuyển sang.

### Cài đặt đã có database

Trước khi khởi động cấu hình Compose có volume mới, xác định chính xác volume
đang chứa `/var/lib/mysql`. Chọn một trong hai phương án:

1. **Tiếp tục dùng volume cũ:** đặt `MYSQL_DATA_VOLUME_NAME` thành đúng tên
   volume đã xác minh và `MYSQL_DATA_VOLUME_EXTERNAL=true`. Compose sẽ dùng
   volume tồn tại đó thay vì tạo volume mới.
2. **Chuyển sang volume mới:** sao lưu database và thực hiện restore có kiểm
   chứng vào named volume mới. Không coi việc khai báo named volume là thao tác
   copy hoặc migrate dữ liệu.

Không khởi động DB với volume mới chưa có dữ liệu trước khi quyết định và kiểm
chứng phương án. Giữ nguyên volume cũ cho đến khi xác nhận thành công.

## 2. Kiểm tra trạng thái trước khi thay đổi

Các lệnh trong mục này chỉ đọc thông tin. Chạy từ máy đang quản lý Docker.

Liệt kê container, kể cả container đã dừng:

```powershell
docker ps -a --filter "name=honeypot_db"
```

In các mount của container DB hiện tại, đặc biệt mount tại `/var/lib/mysql`:

```powershell
docker inspect honeypot_db --format '{{range .Mounts}}{{printf "type=%s name=%s source=%s destination=%s`n" .Type .Name .Source .Destination}}{{end}}'
```

Ghi lại `name` của mount có `destination=/var/lib/mysql`. Nếu `name` rỗng hoặc
mount là bind mount, dừng và xác minh đường dẫn nguồn tương ứng; không tự đoán
tên volume. Nếu container đã bị xóa, kiểm tra các volume và metadata để xác
định volume đúng trước khi làm tiếp:

```powershell
docker volume ls
docker volume inspect <TEN_VOLUME_DA_XAC_MINH>
```

Không chọn volume chỉ dựa vào tên gần giống. Kiểm tra container cũ, labels,
thời gian tạo, mountpoint và quy trình vận hành của máy đó. Lệnh inspect chỉ
hiển thị metadata; không xác minh nội dung database bên trong volume.

## 3. Sao lưu database hiện có

Thực hiện trước khi thay đổi mount hoặc tài khoản. Dừng các dịch vụ ghi dữ liệu
trong thời gian tạo bản sao nhất quán, hoặc dùng quy trình backup đã được quản
trị viên phê duyệt. Với các bảng InnoDB hiện tại, `--single-transaction` tạo
bản sao nhất quán mà không khóa ghi lâu; vẫn nên ngừng Pipeline trong lúc
chuyển đổi để tránh phát sinh chênh lệch giữa backup và cutover.

Ví dụ tạo dump từ database đang chạy:

```powershell
docker exec -it honeypot_db sh
```

Trong shell của container, dùng `mysqldump` với prompt mật khẩu, không đưa mật
khẩu vào command line:

```sh
mysqldump --user=root --password --single-transaction --routines --triggers --events --databases ai_webhoneypot_db > /tmp/ai_webhoneypot_db.sql
exit
```

Chép dump ra vị trí backup được bảo vệ bên ngoài repository:

```powershell
docker cp honeypot_db:/tmp/ai_webhoneypot_db.sql <DUONG_DAN_BACKUP_AN_TOAN>
```

Đảm bảo lệnh hoàn tất, file dump có kích thước hợp lý và có thể đọc/kiểm tra
được trước khi tiếp tục. Bảo vệ file backup như dữ liệu nhạy cảm. Không đặt
backup trong Git repository; không ghi mật khẩu thật vào tài liệu, script,
shell command hoặc `.env.example`.

## 4. Giữ volume cũ hoặc chuyển sang named volume

### Phương án ưu tiên: giữ nguyên volume đang dùng

Sau khi xác minh mount và có backup, cấu hình `.env` cục bộ (không commit) như
sau, thay placeholder bằng đúng tên volume đã xác minh:

```dotenv
MYSQL_DATA_VOLUME_NAME=<TEN_VOLUME_DA_XAC_MINH>
MYSQL_DATA_VOLUME_EXTERNAL=true
```

Giữ nguyên cấu hình MySQL credentials hiện có cho đến khi migration tài khoản
được thực hiện và kiểm chứng. Kiểm tra lại giá trị volume trước khi chạy Compose.
Compose chỉ sử dụng volume được chỉ định; biến sai có thể trỏ tới một volume
khác hoặc khiến Compose báo volume không tồn tại.

### Chuyển dữ liệu sang named volume mới

Chỉ thực hiện nếu có lý do vận hành rõ ràng. Tạo backup trước, xác nhận volume
đích chưa chứa dữ liệu cần giữ, rồi restore bằng một MySQL 8.0 đích độc lập,
không dùng image `Dockerfile.database` của dự án. Image dự án có
`/docker-entrypoint-initdb.d/2_seed_data.sql`; khi khởi tạo đích rỗng, script đó
có thể tự chạy và chứa `TRUNCATE`.

Quy trình mức cao:

1. Ngừng các dịch vụ ghi DB và tạo/kiểm tra dump như mục 3.
2. Tạo một named volume đích mới, dùng đúng phiên bản tương thích MySQL 8.0.
   Không dùng volume cũ làm đích và không ghi đè volume đang chứa dữ liệu.
   Ví dụ lệnh sau tạo một volume mới (thay tên nếu tên đó đã tồn tại):

   ```powershell
   docker volume create <TEN_VOLUME_MIGRATION_MOI>
   ```

3. Khởi tạo một MySQL đích cô lập gắn volume mới, sử dụng image MySQL chuẩn
   không có init script của dự án. Cấp mật khẩu root thông qua cơ chế secret
   hoặc file được bảo vệ; không đưa mật khẩu vào command line.
4. Chờ DB đích sẵn sàng; restore dump vào DB đích; kiểm tra database, bảng,
   số lượng record và quyền trước khi cutover.
5. Dừng DB đích sạch sẽ. Cấu hình `MYSQL_DATA_VOLUME_NAME` thành đúng tên
   volume đích và `MYSQL_DATA_VOLUME_EXTERNAL=true` để Compose dùng volume đã
   tạo. Chỉ sau khi kiểm chứng ứng dụng mới cân nhắc dọn tài nguyên cũ theo quy
   trình vận hành riêng.

Không khởi tạo volume đích bằng service `database` hiện tại nếu chưa xử lý rõ
cơ chế tự chạy seed. Không dùng `seed_data.sql` như migration. Nếu chưa có
quy trình restore/rollback đã được thử nghiệm trên môi trường phù hợp, giữ
nguyên volume cũ là phương án an toàn hơn.

## 5. Tạo hoặc đồng bộ tài khoản ứng dụng hiện có

Kết nối với database hiện tại bằng tài khoản quản trị có quyền quản lý user.
Nhập lệnh trong phiên quản trị được bảo vệ và không lưu mật khẩu vào lịch sử
shell/client. Tài khoản Pipeline và AI Engine đang dùng chung các biến
`MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_HOST` và `MYSQL_PORT`.

Nếu cần chuẩn hóa lại account trên host `%`, tạo user nếu chưa có rồi đồng bộ
mật khẩu. Thay placeholder bằng cùng secret được cấu hình cho ứng dụng; không
đưa secret thật vào file migration hoặc Git:

```sql
CREATE USER IF NOT EXISTS 'honeypot_pipeline'@'%' IDENTIFIED BY '<SECRET_QUAN_TRI_VIEN_CUNG_CAP>';
ALTER USER 'honeypot_pipeline'@'%' IDENTIFIED BY '<SECRET_QUAN_TRI_VIEN_CUNG_CAP>';
GRANT SELECT, INSERT ON ai_webhoneypot_db.raw_logs TO 'honeypot_pipeline'@'%';
GRANT SELECT, INSERT ON ai_webhoneypot_db.ai_labels TO 'honeypot_pipeline'@'%';
GRANT SELECT, INSERT ON ai_webhoneypot_db.pentest_payloads TO 'honeypot_pipeline'@'%';
SHOW GRANTS FOR 'honeypot_pipeline'@'%';
```

`CREATE USER IF NOT EXISTS` không cập nhật một account đã tồn tại; vì vậy bước
`ALTER USER` là cần thiết để đồng bộ mật khẩu. Kiểm tra account hiện tại và
quyền trước khi áp dụng. Nếu môi trường cần giới hạn host chặt hơn `%`, xác
định đúng nguồn kết nối từ các container và dùng account host tương ứng thay
vì cấp rộng tùy tiện.

Các truy vấn hiện tại cần `SELECT` và `INSERT` trên ba bảng được nêu. Không cấp
`ALL PRIVILEGES`, `CREATE`, `ALTER`, `DROP`, `UPDATE` hoặc `DELETE` cho ứng
dụng nếu chưa có yêu cầu chức năng và kế hoạch riêng.

## 6. Xác minh sau migration

So sánh trước/sau trên cùng database:

```sql
SELECT SCHEMA_NAME
FROM INFORMATION_SCHEMA.SCHEMATA
WHERE SCHEMA_NAME = 'ai_webhoneypot_db';

SELECT TABLE_NAME
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'ai_webhoneypot_db'
ORDER BY TABLE_NAME;

SELECT 'raw_logs' AS table_name, COUNT(*) AS row_count
FROM ai_webhoneypot_db.raw_logs
UNION ALL
SELECT 'ai_labels', COUNT(*) FROM ai_webhoneypot_db.ai_labels
UNION ALL
SELECT 'pentest_payloads', COUNT(*) FROM ai_webhoneypot_db.pentest_payloads;
```

Đối chiếu kết quả với snapshot trước cutover; nếu hệ thống được phép ghi trong
quá trình đó, giải thích chênh lệch bằng thời điểm backup và các event mới.
Xác minh thêm khóa ngoại/metadata nếu cần. Kiểm tra mount sau khi Compose chạy:

```powershell
docker inspect honeypot_db --format '{{range .Mounts}}{{printf "type=%s name=%s destination=%s`n" .Type .Name .Destination}}{{end}}'
```

Xác minh quyền user bằng `SHOW GRANTS` và thử kết nối ứng dụng từ các container
sau khi operator đã thực hiện migration. Có thể kiểm tra kết nối cấu hình từ
Pipeline bằng lệnh chỉ đọc:

```powershell
docker compose exec log_pipeline python -c "import mysql.connector; from config import get_mysql_config; c=mysql.connector.connect(**get_mysql_config()); cur=c.cursor(); cur.execute('SELECT 1'); print(cur.fetchone()); cur.close(); c.close()"
```

Thực hiện kiểm tra tương đương trong container AI Engine nếu cần. Lệnh kết nối
ở trên không tạo record; xác minh luồng ghi bằng kiểm thử ứng dụng đã được
phê duyệt riêng.

## 7. Rollback và cảnh báo

- Không xóa volume cũ trước khi mount mới, database, bảng, record, quyền user
  và kết nối ứng dụng đều được xác minh.
- Giữ dump backup ở nơi an toàn cho tới khi có backup mới đã kiểm chứng.
- Nếu xác minh thất bại, dừng dịch vụ ghi và quay lại cấu hình volume cũ đã ghi
  nhận; không tiếp tục thử volume bằng cách khởi tạo lại database.
- Không chạy `seed_data.sql` trên database hiện có. Không dùng lệnh xóa/prune
  volume trong quy trình migration này.
- Không chạy `docker compose down -v`, `docker volume prune` hoặc lệnh tương
  tự có thể xóa volume.
- Hướng dẫn này không thực hiện backup, restore, thay đổi user, cấp quyền hay
  thao tác Docker; operator phải kiểm tra từng bước và có phương án khôi phục.

## 8. Migration idempotency cho Pipeline

Migration `001_raw_logs_idempotency.sql` bổ sung ba cột nullable và unique
index vào `raw_logs`. Cài đặt mới nhận cùng cấu trúc qua `schema.sql`.
`ingestion_key_hash` là SHA-256 dạng `BINARY(32)` của canonical
`ingestion_key`; `ingestion_key_bytes` lưu chính xác chuỗi key UTF-8 để phát
hiện trường hợp cùng hash nhưng key khác; `source_record_sha256` là SHA-256
dạng `BINARY(32)` tính từ raw record bytes nguyên bản trong Inbox, bao gồm byte
newline nếu có.

Migration không backfill và không sửa các hàng lịch sử: các giá trị idempotency
của những hàng đó giữ `NULL`. Không đối chiếu event cũ bằng IP, payload hoặc
timestamp. Unique index chỉ bảo vệ các event mới có ingestion key; nó không
chứng minh một record được đọc lại từ đầu chưa từng có trong các hàng lịch sử.

### Preflight và áp dụng

1. Dừng Pipeline và mọi writer liên quan trong thời gian cutover. Xác minh đúng
   DB/volume và MySQL 8.0 thực tế; Compose chỉ khai báo image MySQL 8.0.
2. Tạo backup nhất quán theo mục 3, kiểm tra file backup, và thử restore vào DB
   test cô lập trước khi áp dụng trên DB cần giữ.
3. Kiểm tra schema của `raw_logs` để bảo đảm chưa có các cột/index này dưới tên
   khác. Xác minh ứng dụng có quyền `ALTER TABLE` trong phiên triển khai được
   kiểm soát; không cấp quyền DDL lâu dài cho Pipeline.
4. Chạy `001_raw_logs_idempotency.sql` đúng một lần từ phiên quản trị. Không
   thêm migration vào Docker entrypoint, lệnh khởi động Pipeline hoặc mỗi lần
   container restart. Migration phải được ghi nhận trong migration ledger/
   release record do operator quản lý.
5. Đọc `INFORMATION_SCHEMA.COLUMNS` và `INFORMATION_SCHEMA.STATISTICS` để xác
   minh kiểu, nullability, unique index và tên index. Đối chiếu số lượng hàng
   trước/sau; không chạy seed trên DB hiện hữu.
6. Deploy Pipeline đã truyền ingestion key và source hash, sau đó chạy kiểm
   thử integration trên DB test riêng. Chỉ mở watcher sau khi schema và binary
   đã được xác minh tương thích.

DDL này cố ý không dùng `IF NOT EXISTS` vì không phải mọi patch MySQL 8.0 đều
có cùng hỗ trợ cho cú pháp đó. Nếu lệnh trả lỗi hoặc kết nối mất trong khi DDL
đang chạy, **không chạy lại ngay**: đọc trạng thái cột/index qua
`INFORMATION_SCHEMA`, ghi nhận cấu hình thực tế và dừng nếu schema ở trạng
thái một phần hoặc không khớp. Chỉ migration operator mới được quyết định
resume/repair; không tự drop cột, index, bảng hoặc volume.

### Tính nhất quán và giới hạn

Các hàng mới lưu raw log, AI label và pentest payload (nếu risk yêu cầu) trong
cùng transaction. Unique index trên key hash là hàng rào khi hai consumer cùng
insert; phía nhận duplicate phải rollback rồi xác minh canonical key bytes,
source hash và các hàng con trước khi đánh dấu Inbox `PROCESSED`. Integrity conflict
được giữ lại trong Inbox và ghi sidecar `REJECTED`; cần operator điều tra trước
khi xử lý lại, không tự xóa Inbox event hoặc sidecar.

MySQL DDL có thể cần metadata lock và build index; thời gian/ảnh hưởng phụ
thuộc kích thước bảng, tải và patch version. Đo trên bản sao có quy mô gần thực
tế. Rollback ứng dụng nên giữ cột/index. Phiên bản Pipeline cũ có thể ghi các
cột mới là `NULL`, nên rollback code không giữ bảo đảm idempotency; dừng ingest
cho tới khi phiên bản tương thích được khôi phục.

`from-start` chỉ an toàn khi database được xác nhận rỗng hoặc có quy trình
reconciliation event-level chính xác được duyệt. Hàng lịch sử không có key
không thể đối chiếu an toàn bằng IP/payload fingerprint. Nếu không xác định
được overlap, không tuyên bố backfill từ đầu là không duplicate; cân nhắc
chính sách bắt đầu từ cuối log hoặc trì hoãn backfill.

## 9. Migration lưu recommendation của AI

Schema cài mới khai báo `ai_labels.recommendation` là `TEXT NULL`. Với database
đã tồn tại, migration `002_ai_label_recommendation.sql` thêm cột này; không tự
chạy khi khởi động ứng dụng. Các recommendation cũ không được tạo lại và giữ
`NULL`, vì chúng chưa được lưu trong database.

Trước khi áp dụng, dừng Pipeline và các writer, xác minh database/volume đích,
tạo backup nhất quán và thử restore vào môi trường cô lập. Kiểm tra
`INFORMATION_SCHEMA.COLUMNS` để chắc chắn cột chưa tồn tại; nếu đã tồn tại,
đối chiếu kiểu và nullability rồi dừng, không chạy lại DDL. Áp dụng migration
một lần bằng tài khoản quản trị có quyền DDL, ghi nhận phiên bản trong
migration ledger và không cấp quyền DDL thường trực cho Pipeline.

Sau khi áp dụng, xác minh `ai_labels.recommendation` có kiểu `TEXT` và nullable
bằng `INFORMATION_SCHEMA.COLUMNS`. Kiểm tra số lượng hàng trước/sau không đổi
và các hàng cũ vẫn có `NULL`; sau đó chỉ kiểm tra luồng ghi/đọc trên database
test riêng trước khi mở ingest. Không chạy migration trên database hiện tại
trong khuôn khổ thay đổi ứng dụng này.
