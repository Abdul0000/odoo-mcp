# ChatGPT hard tool-name fix

Use `/chatgpt/mcp` when ChatGPT still shows:

```
tools.0.FrontendRemoteMcpToolDefinition.name: String should match pattern
```

This endpoint uses the same OAuth/auth and same tool dispatch as `/mcp`, but serializes a minimal strict MCP tool list with names matching `^[a-zA-Z0-9_-]{1,64}$`.

Recommended ChatGPT URL:

```
https://YOUR-SERVICE.onrender.com/chatgpt/mcp
```

Debug after deploy:

```
GET /chatgpt/tools-debug
```

It should return `bad_names: []`.
