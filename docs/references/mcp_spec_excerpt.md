# MCP Protocol Reference (2025-06-18)

Relevant excerpts from the Model Context Protocol specification for
implementing the libby-mcp server.

## Streamable HTTP Transport (Request-Response Subset)

For the Lambda-based MVP, the server implements only the request-response
subset of Streamable HTTP:

- Single `POST /mcp` endpoint accepts JSON-RPC 2.0 requests
- Returns immediate JSON-RPC 2.0 responses (no SSE)
- No persistent connections (Lambda is stateless)
- No `Mcp-Session-Id` session management
- Each request is independent

## JSON-RPC 2.0 Protocol

All MCP messages are JSON-RPC 2.0:

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {...}}
```

Responses:
```json
{"jsonrpc": "2.0", "id": 1, "result": {...}}
```

Errors:
```json
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}}
```

## Lifecycle Methods

### initialize

Client sends first. Server returns capabilities:
```json
{
  "protocolVersion": "2025-06-18",
  "capabilities": {"tools": {}},
  "serverInfo": {"name": "libby-mcp", "version": "1.0.0"}
}
```

### notifications/initialized

Client sends after receiving initialize response. Server acknowledges
(no response body for notifications -- they have no `id` field).

### ping

Standard lifecycle method. Server returns empty result: `{}`

## Tool Methods

### tools/list

Returns array of tool definitions:
```json
{
  "tools": [
    {
      "name": "search_titles",
      "description": "...",
      "inputSchema": {
        "type": "object",
        "properties": {...},
        "required": [...]
      }
    }
  ]
}
```

### tools/call

Invokes a tool:
```json
{
  "method": "tools/call",
  "params": {
    "name": "search_titles",
    "arguments": {"library_slug": "lcpl", "subjects": [24]}
  }
}
```

Success response:
```json
{
  "content": [
    {"type": "text", "text": "{\"titles\": [...]}"}
  ]
}
```

Error response (tool-level, not protocol-level):
```json
{
  "content": [
    {"type": "text", "text": "Error: Library 'invalid' not found"}
  ],
  "isError": true
}
```

## HTTP Status Codes

| Scenario | Status |
|----------|--------|
| JSON-RPC request (has `id`) | 200 OK |
| Notification only (no `id`) | 202 Accepted (empty body) |
| GET /mcp | 404 Not Found |
| DELETE /mcp | 404 Not Found |

## Error Codes (JSON-RPC)

| Code | Meaning |
|------|---------|
| -32700 | Parse error (invalid JSON) |
| -32600 | Invalid request (missing required fields) |
| -32601 | Method not found |
| -32602 | Invalid params |
| -32603 | Internal error |
