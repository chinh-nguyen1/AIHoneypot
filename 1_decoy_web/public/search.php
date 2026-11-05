<?php
require_once __DIR__ . '/../config/request_logger.php';
honeypot_log_http_request();

$query = $_GET['q'] ?? '';

?>
<!DOCTYPE html>
<html lang="vi">
<head><meta charset="UTF-8"><title>TechStore - Tìm kiếm</title></head>
<body style="font-family:Arial; padding:40px;">
    <h2>Tìm kiếm sản phẩm TechStore</h2>
    <form method="GET">
        <input type="text" name="q" value="<?= $query ?>" placeholder="Nhập tên sản phẩm..." style="padding:10px; width:300px;">
        <button type="submit" style="padding:10px 15px; background:#28a745; color:#fff; border:none;">Tìm kiếm</button>
    </form>
    <?php if ($query): ?>
        <!-- CỐ Ý IN TRỰC TIẾP KHÔNG ESCAPE ĐỂ TẠO BẪY REFLECTED XSS -->
        <p style="margin-top:20px;">Kết quả cho: <b><?= $query ?></b></p>
    <?php endif; ?>
</body>
</html>