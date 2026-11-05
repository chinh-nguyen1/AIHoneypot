<?php
require_once __DIR__ . '/../config/request_logger.php';
honeypot_log_http_request();

require_once '../config/db.php';

$message = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $fullname = $_POST['fullname'] ?? '';
    $email = $_POST['email'] ?? '';
    $password = $_POST['password'] ?? '';

    $message = "<div style='color:green;'>Đăng ký tài khoản thành công! <a href='login.php'>Đăng nhập ngay</a></div>";
}
?>
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <title>TechStore - Đăng Ký Tài Khoản</title>
</head>
<body style="font-family: Arial; padding: 50px; background: #f8f9fa;">
    <div style="max-width: 400px; margin: 0 auto; background: #fff; padding: 25px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
        <h2>Tạo Tài Khoản TechStore</h2>
        <?= $message ?>
        <form method="POST">
            <p><input type="text" name="fullname" placeholder="Họ và tên" style="width: 100%; padding: 10px; box-sizing: border-box;" required></p>
            <p><input type="email" name="email" placeholder="Địa chỉ Email" style="width: 100%; padding: 10px; box-sizing: border-box;" required></p>
            <p><input type="password" name="password" placeholder="Mật khẩu" style="width: 100%; padding: 10px; box-sizing: border-box;" required></p>
            <button type="submit" style="width: 100%; padding: 10px; background: #28a745; color: #fff; border: none; border-radius: 4px; font-weight: bold;">Đăng ký</button>
        </form>
        <p style="text-align: center; font-size: 14px;"><a href="login.php">Đã có tài khoản? Đăng nhập</a></p>
    </div>
</body>
</html>