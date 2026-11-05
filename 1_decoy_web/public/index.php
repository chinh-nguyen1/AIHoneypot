<?php
require_once __DIR__ . '/../config/request_logger.php';
honeypot_log_http_request();

require_once '../config/db.php';

// Danh sách sản phẩm mẫu TechStore
$products = [
    ['id' => 1, 'name' => 'iPhone 15 Pro Max 256GB', 'price' => '32.990.000 VNĐ', 'img' => 'https://via.placeholder.com/200?text=iPhone+15'],
    ['id' => 2, 'name' => 'MacBook Pro M3 14-inch', 'price' => '39.990.000 VNĐ', 'img' => 'https://via.placeholder.com/200?text=MacBook+M3'],
    ['id' => 3, 'name' => 'Tai nghe Sony WH-1000XM5', 'price' => '8.490.000 VNĐ', 'img' => 'https://via.placeholder.com/200?text=Sony+Headphone'],
    ['id' => 4, 'name' => 'Đồng hồ Apple Watch Series 9', 'price' => '10.290.000 VNĐ', 'img' => 'https://via.placeholder.com/200?text=Apple+Watch']
];
?>
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <title>TechStore - Cửa Hàng Công Nghệ Hàng Đầu</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; background: #f4f6f9; }
        header { background: #007bff; color: #fff; padding: 15px 30px; display: flex; justify-content: space-between; align-items: center; }
        header a { color: #fff; text-decoration: none; margin-left: 15px; font-weight: bold; }
        .container { max-width: 1100px; margin: 30px auto; padding: 0 15px; }
        .banner { background: #e9ecef; padding: 30px; border-radius: 8px; text-align: center; margin-bottom: 30px; }
        .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; }
        .card { background: #fff; border-radius: 8px; padding: 15px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        .card img { max-width: 100%; border-radius: 4px; }
        .card h4 { margin: 10px 0 5px 0; font-size: 16px; }
        .card p { color: #dc3545; font-weight: bold; margin: 5px 0 15px 0; }
        .btn { background: #28a745; color: #fff; text-decoration: none; padding: 8px 15px; border-radius: 4px; display: inline-block; }
    </style>
</head>
<body>
    <header>
        <h2>🛒 TechStore</h2>
        <nav>
            <a href="index.php">Trang chủ</a>
            <a href="search.php">Tìm kiếm</a>
            <a href="cart.php">Giỏ hàng</a>
            <a href="login.php">Đăng nhập</a>
            <a href="register.php">Đăng ký</a>
        </nav>
    </header>

    <div class="container">
        <div class="banner">
            <h1>Chào Mừng Đến Với TechStore</h1>
            <p>Khuyến mãi siêu khủng giảm giá đến 30% cho các thiết bị công nghệ mới nhất!</p>
        </div>

        <h3>🔥 Sản Phẩm Nổi Bật</h3>
        <div class="grid">
            <?php foreach ($products as $p): ?>
                <div class="card">
                    <img src="<?= $p['img'] ?>" alt="<?= $p['name'] ?>">
                    <h4><?= $p['name'] ?></h4>
                    <p><?= $p['price'] ?></p>
                    <a href="product_detail.php?id=<?= $p['id'] ?>" class="btn">Xem Chi Tiết</a>
                </div>
            <?php endforeach; ?>
        </div>
    </div>
</body>
</html>