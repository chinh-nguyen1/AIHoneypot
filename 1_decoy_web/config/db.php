<?php
$host = 'database'; 
$user = 'root';
$pass = 'rootpassword';
$dbname = 'ai_webhoneypot_db';

$conn = new mysqli($host, $user, $pass, $dbname);

if ($conn->connect_error) {
    die("Kết nối CSDL thất bại: " . $conn->connect_error);
}
?>