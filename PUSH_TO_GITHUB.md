# 将 v1.3.0-web 源码更新到仓库

本包是完整源代码，不是已发布的 Docker 镜像。基于 `jsdfhasuh/AI-vision-tcp-server` 的 `1d3992d6f06def52b1221a6e557d5b6dc164eeab`。本次没有执行远端写入。

在你已有的本地仓库中先 `git status`，处理自己的未提交改动并备份数据。建议创建独立分支再导入：

```bash
git switch -c feature/web-docker-v1.3
```

把本包 `AI-vision-tcp-server` 文件夹里的内容复制到仓库根目录，不要多套一层。保留现有 `.git`、`.env`、`secrets/` 和数据，不把备份/运行数据复制进源码。然后检查并提交：

```bash
git status --short
git add .
git diff --cached --stat
git commit -m "feat: add pure web referee and Docker deployment"
git push -u origin feature/web-docker-v1.3
```

上述命令推送的是新分支，不自动合并main。任何一步报错先停止，不使用force。服务器要运行本版时必须取得包含 `web_server.py`、`Dockerfile` 和 `compose.yaml` 的这个分支或本源码包，不要误用旧版main。

上传前确认没有 `.sqlite*`、实际 `.db*`、凭据/私钥、备份、导出及日志被暂存。`.gitignore` 和 `.dockerignore` 是辅助措施，不代替人工检查。Windows脚本原样保留，新增Linux/Docker相关文本使用LF换行。
