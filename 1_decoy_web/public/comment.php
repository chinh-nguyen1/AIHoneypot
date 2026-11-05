<?php
require_once __DIR__ . '/../config/request_logger.php';
honeypot_log_http_request();

require_once '../config/db.php';

// Tải danh sách bình luận đã lưu từ file JSON
$comments_file = '../logs/comments.json';
$comments = [];
if (is_file($comments_file)) {
    $comments_json = file_get_contents($comments_file);
    if ($comments_json === false) {
        error_log('AI-WebHoneypot could not read the comments file: ' . $comments_file);
    } else {
        $decoded_comments = json_decode($comments_json, true);
        if (is_array($decoded_comments)) {
            $comments = $decoded_comments;
        } elseif (json_last_error() !== JSON_ERROR_NONE) {
            error_log('AI-WebHoneypot comments file contains invalid JSON: ' . $comments_file);
        } else {
            error_log('AI-WebHoneypot comments file does not contain a JSON array: ' . $comments_file);
        }
    }
}

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $author = $_POST['author'] ?? 'Khách';
    $content = $_POST['content'] ?? '';

    // Keep the original value for honeypot analysis; escape it only when rendered.
    if (!empty($content)) {
        $comments[] = ['author' => $author, 'content' => $content, 'time' => date('H:i d/m/Y')];
        file_put_contents($comments_file, json_encode($comments));
    }
}

function honeypot_escape_comment_output($value): string
{
    if (!is_scalar($value)) {
        return '';
    }

    return htmlspecialchars((string) $value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
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
                    <b><?= honeypot_escape_comment_output($c['author'] ?? '') ?></b> <small style="color: #888;">(<?= honeypot_escape_comment_output($c['time'] ?? '') ?>)</small>
                    <p style="margin: 5px 0 0 0;"><?= honeypot_escape_comment_output($c['content'] ?? '') ?></p>
                </div>
            <?php endforeach; ?>
        <?php else: ?>
            <p style="color: #888;">Chưa có bình luận nào. Hãy là người đầu tiên đánh giá!</p>
        <?php endif; ?>
    </div>
</body>
</html>