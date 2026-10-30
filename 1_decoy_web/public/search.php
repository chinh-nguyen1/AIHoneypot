<?php
$query = $_GET['q'] ?? '';

if ($query) {
    $log_entry = sprintf(
        "[%s] IP: %s | Method: GET | URI: /search.php?q=%s | Payload: %s\n",
        date('Y-m-d H:i:s'),
        $_SERVER['REMOTE_ADDR'],
        urlencode($query),
        $query
    );
    file_put_contents('../logs/access.log', $log_entry, FILE_APPEND);
}
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