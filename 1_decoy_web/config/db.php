<?php
$databaseConfig = [];

foreach (['MYSQL_HOST', 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE'] as $name) {
    $value = getenv($name);

    if ($value === false || $value === '') {
        http_response_code(500);
        $message = "Database configuration error: required environment variable {$name} is missing.";
        error_log($message);
        die($message);
    }

    $databaseConfig[$name] = $value;
}

$portValue = getenv('MYSQL_PORT');
if ($portValue === false || $portValue === '') {
    $port = 3306;
} elseif (!ctype_digit($portValue) || (int) $portValue < 1 || (int) $portValue > 65535) {
    http_response_code(500);
    error_log('Database configuration error: MYSQL_PORT must be a valid TCP port.');
    die('Database configuration error: MYSQL_PORT must be a valid TCP port.');
} else {
    $port = (int) $portValue;
}

try {
    $conn = new mysqli(
        $databaseConfig['MYSQL_HOST'],
        $databaseConfig['MYSQL_USER'],
        $databaseConfig['MYSQL_PASSWORD'],
        $databaseConfig['MYSQL_DATABASE'],
        $port
    );
} catch (mysqli_sql_exception $exception) {
    error_log('Database connection failed. Check database service availability and configuration.');
    http_response_code(500);
    die('Database connection failed. Check the server configuration.');
}

if ($conn->connect_error) {
    error_log('Database connection failed. Check database service availability and configuration.');
    http_response_code(500);
    die('Database connection failed. Check the server configuration.');
}
?>