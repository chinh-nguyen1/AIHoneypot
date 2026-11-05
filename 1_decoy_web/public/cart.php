<?php
require_once __DIR__ . '/../config/request_logger.php';
honeypot_log_http_request();

require_once '../config/db.php';

$order_code = $_GET['order_code'] ?? '';
$search_result = '';

if ($order_code) {
    // CỐ Ý BỎ LỌC CÂU LỆNH SQL ĐỂ TẠO MỒI NHỬ SQL INJECTION TRA CỨU ĐƠN HÀNG
    $sql = "SELECT * FROM raw_logs WHERE client_ip = '$order_code'";
    $conn->query($sql);
    
    $search_result = "<div style='color:red; margin-top:15px;'>Mã đơn hàng '$order_code' không tồn tại trên hệ thống!</div>";
}
?>
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <title>TechStore - Giỏ Hàng & Tra Cứu Đơn Hàng</title>
</head>
<body style="font-family: Arial; padding: 40px; background: #f4f6f9;">
    <div style="max-width: 700px; margin: 0 auto; background: #fff; padding: 25px; border-radius: 8px;">
        <h2>🛒 Giỏ Hàng Của Bạn</h2>
        <p style="color: #666;">Hiện chưa có sản phẩm nào trong giỏ hàng. <a href="index.php">Tiếp tục mua sắm</a></p>
        
        <hr style="margin: 30px 0; border: none; border-top: 1px solid #ddd;">
        
        <h3>📦 Tra Cứu Trạng Thái Đơn Hàng</h3>
        <form method="GET">
            <input type="text" name="order_code" value="<?= $order_code ?>" placeholder="Nhập mã đơn hàng (VD: ORD12345)" style="padding: 10px; width: 350px;">
            <button type="submit" style="padding: 10px 20px; background: #007bff; color: #fff; border: none; border-radius: 4px;">Tra cứu</button>
        </form>
        <?= $search_result ?>
    </div>
</body>
</html>