# 本地 Compose secrets

本目录中的 `*.dev` 仅用于让本地空环境可直接启动，属于公开开发凭据，不能用于
staging/production。生产部署必须通过 `MCP_*_FILE_PATH` 指向部署系统创建的独立 secret
文件；五个服务 token 不能复用，上下文 HMAC key 也不能与服务 token 相同。
