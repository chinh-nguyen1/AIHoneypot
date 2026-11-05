<?php
require_once __DIR__ . '/../config/request_logger.php';
honeypot_log_http_request();

require_once '../config/db.php';

$message = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $username = $_POST['username'] ?? '';
    $password = $_POST['password'] ?? '';

    // CỐ Ý KHÔNG FILTER ĐỂ TẠO BẪY SQLi
    $sql = "SELECT * FROM raw_logs WHERE raw_payload = '$username'";
    $result = $conn->query($sql);
    
    $message = "<div style='color:red;'>Tài khoản hoặc mật khẩu không chính xác!</div>";
}
?>
<!DOCTYPE html>
<html lang="vi">
<head><meta charset="UTF-8"><title>TechStore - Đăng nhập</title></head>
<body style="font-family:Arial; padding:50px; background:#f8f9fa;">
    <div style="max-width:380px; margin:0 auto; background:#fff; padding:25px; border-radius:8px; box-shadow:0 2px 10px rgba(0,0,0,0.1);">
        <h2>TechStore Login</h2>
        <?= $message ?>
        <form method="POST">
            <p><input type="text" name="username" placeholder="Tên đăng nhập / Email" style="width:100%; padding:10px; box-sizing:border-box;" required></p>
            <p><input type="password" name="password" placeholder="Mật khẩu" style="width:100%; padding:10px; box-sizing:border-box;" required></p>
            <button type="submit" style="width:100%; padding:10px; background:#007bff; color:#fff; border:none; border-radius:4px; font-weight:bold;">Đăng nhập</button>
        </form>
    </div>
</body>
</html>