<?php

function honeypot_log_filter_sensitive_params(array $params): array
{
    $filtered = [];

    foreach ($params as $key => $value) {
        $normalizedKey = strtolower((string) preg_replace('/[^a-z0-9]/i', '', (string) $key));

        if (preg_match('/(?:pass(?:word|wd)?|pwd|token|secret|authorization|cookie|api.?key|access.?key|credential|private.?key|session(?:id)?|^auth$|jwt)/i', $normalizedKey)) {
            continue;
        }

        $filtered[$key] = is_array($value)
            ? honeypot_log_filter_sensitive_params($value)
            : $value;
    }

    return $filtered;
}

function honeypot_log_http_request(): ?string
{
    static $attempted = false;
    static $sourceEventId = null;

    if ($attempted) {
        return $sourceEventId;
    }

    $attempted = true;

    try {
        $sourceEventId = bin2hex(random_bytes(16));
    } catch (Throwable $exception) {
        error_log('AI-WebHoneypot request logger could not generate an event ID: ' . $exception->getMessage());
        return null;
    }

    $queryParams = honeypot_log_filter_sensitive_params($_GET);
    $bodyParams = honeypot_log_filter_sensitive_params($_POST);
    $uri = (string) ($_SERVER['SCRIPT_NAME'] ?? '');

    if ($uri === '') {
        $requestUri = (string) ($_SERVER['REQUEST_URI'] ?? '');
        $path = parse_url($requestUri, PHP_URL_PATH);
        $uri = is_string($path) ? $path : '';
    }

    $record = [
        'source_event_id' => $sourceEventId,
        'timestamp' => date(DATE_ATOM),
        'source_ip' => $_SERVER['REMOTE_ADDR'] ?? null,
        'method' => strtoupper((string) ($_SERVER['REQUEST_METHOD'] ?? 'GET')),
        'uri' => $uri,
        'query_params' => $queryParams,
        'body_params' => $bodyParams,
        'payload' => [
            'query_params' => $queryParams,
            'body_params' => $bodyParams,
        ],
        'user_agent' => $_SERVER['HTTP_USER_AGENT'] ?? null,
        'request_type' => 'web',
    ];

    try {
        $json = json_encode(
            $record,
            JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE | JSON_THROW_ON_ERROR
        );
    } catch (JsonException $exception) {
        error_log('AI-WebHoneypot request logger could not encode request record: ' . $exception->getMessage());
        return $sourceEventId;
    }

    $logPath = dirname(__DIR__) . '/logs/access.log';
    $line = $json . PHP_EOL;
    error_clear_last();
    $written = @file_put_contents($logPath, $line, FILE_APPEND | LOCK_EX);

    if ($written !== strlen($line)) {
        $error = error_get_last();
        $details = $error['message'] ?? 'incomplete write';
        error_log('AI-WebHoneypot request logger could not write a complete record to ' . $logPath . ': ' . $details);
    }

    return $sourceEventId;
}
