<?php
require_once '../config/db.php';

// Tải danh sách bình luận đã lưu từ file JSON
$comments_file = '../logs/comments.json';
$comments = file_exists($comments_file) ? json_decode(file_get_content($comments_file), true) : [];

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $author = $_POST['author'] ?? 'Khách';
    $content = $_POST['content'] ?? '';

    // Ghi log hành vi gửi bình luận
    $log_entry = sprintf(
        "[%s] IP: %s | Method: POST | URI: /comment.php | Payload: author=%s&content=%s\n",
        date('Y-m-d H:i:s'),
        $_SERVER['REMOTE_ADDR'],
        $author,
        $content
    );
    file_put_contents('../logs/access.log', $log_entry, FILE_APPEND);

    // CỐ Ý LƯU TRỰC TIẾP CHUỖI KHÔNG SANITIZE ĐỂ TẠO BẪY STORED XSS
    if (!empty($content)) {
        $comments[] = ['author' => $author, 'content' => $content, 'time' => date('H:i d/m/Y')];
        file_put_contents($comments_file, json_encode($comments));
    }
}
?>
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <title>TechStore - Đánh Giá Sản Phẩm</title>
</head>
<body style="font-family: Arial; padding: 40px; background: #f4f6f9;">
    <div style="max-width: 650px; margin: 0 auto; background: #fff; padding: 25px; border-radius: 8px;">
        <h2>💬 Đánh Giá & Bình Luận Sản Phẩm</h2>
        
        <form method="POST" style="margin-bottom: 30px;">
            <p><input type="text" name="author" placeholder="Tên của bạn" style="width: 100%; padding: 8px; box-sizing: border-box;" required></p>
            <p><textarea name="content" rows="4" placeholder="Viết đánh giá của bạn (VD: Sản phẩm rất tốt...)" style="width: 100%; padding: 8px; box-sizing: border-box;" required></textarea></p>
            <button type="submit" style="padding: 10px 20px; background: #28a745; color: #fff; border: none; border-radius: 4px;">Gửi đánh giá</button>
        </form>

        <h3>Khách Hàng Nói Gì:</h3>
        <?php if (!empty($comments)): ?>
            <?php foreach (array_reverse($comments) as $c): ?>
                <div style="background: #f8f9fa; padding: 12px; margin-bottom: 10px; border-left: 4px solid #007bff; border-radius: 4px;">
                    <b><?= $c['author'] ?></b> <small style="color: #888;">(<?= $c['time'] ?>)</small>
                    <!-- CỐ Ý IN KHÔNG ESCAPE HTMLSPECIALCHARS ĐỂ KÍCH HOẠT STORED XSS -->
                    <p style="margin: 5px 0 0 0;"><?= $c['content'] ?></p> 
                </div>
            <?php endforeach; ?>
        <?php else: ?>
            <p style="color: #888;">Chưa có bình luận nào. Hãy là người đầu tiên đánh giá!</p>
        <?php endif; ?>
    </div>
</body>
</html>