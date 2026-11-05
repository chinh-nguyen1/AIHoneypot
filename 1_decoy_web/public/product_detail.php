<?php
require_once __DIR__ . '/../config/request_logger.php';
honeypot_log_http_request();

require_once '../config/db.php';

$product_id = $_GET['id'] ?? '1';

// CỐ Ý BỎ LỌC ĐỂ TẠO MỒI NHỬ SQL INJECTION QUA THAM SỐ GET ?id=
$sql = "SELECT * FROM raw_logs WHERE id = $product_id"; 
$conn->query($sql); // Chạy query mồi
?>
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <title>TechStore - Chi Tiết Sản Phẩm</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f4f6f9; margin: 0; }
        .container { max-width: 800px; margin: 40px auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .btn-back { background: #6c757d; color: #fff; text-decoration: none; padding: 8px 15px; border-radius: 4px; }
        .btn-buy { background: #007bff; color: #fff; border: none; padding: 12px 20px; border-radius: 4px; font-weight: bold; cursor: pointer; }
    </style>
</head>
<body>
    <div class="container">
        <a href="index.php" class="btn-back">← Quay lại trang chủ</a>
        <h2 style="margin-top: 20px;">iPhone 15 Pro Max 256GB</h2>
        <img src="https://via.placeholder.com/300?text=iPhone+15+Pro+Max" style="float: left; margin-right: 30px; border-radius: 8px;">
        <div>
            <p><b>Giá bán:</b> <span style="color: #dc3545; font-size: 22px; font-weight: bold;">32.990.000 VNĐ</span></p>
            <p><b>Thương hiệu:</b> Apple</p>
            <p><b>Bảo hành:</b> 12 tháng chính hãng</p>
            <p><b>Mô tả:</b> Chip A17 Pro mạnh mẽ, khung vỏ Titan siêu bền nhẹ, nút Action Button tiện lợi.</p>
            <br>
            <button class="btn-buy" onclick="alert('Đã thêm sản phẩm vào giỏ hàng!')">🛒 Thêm Vào Giỏ Hàng</button>
            <a href="comment.php?product_id=<?= $product_id ?>" style="margin-left: 15px; color: #007bff;">Xem bình luận & đánh giá</a>
        </div>
    </div>
</body>
</html>