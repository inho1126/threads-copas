export function sendJson(response, statusCode, value) {
  const body = JSON.stringify(value);

  response.writeHead(statusCode, {
    'content-length': Buffer.byteLength(body),
    'content-type': 'application/json; charset=utf-8',
  });
  response.end(body);
}
