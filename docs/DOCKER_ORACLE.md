# Oracle / Linux：纯 Web Docker 部署

版本：1.3.0-web。适用于能够运行 Docker Engine + Compose v2 的 Linux 主机。本文不假定你的 Oracle SSH 用户名、发行版、域名或 CPU 架构；用 `uname -m` 和 `/etc/os-release` 在实际主机确认。

**本交付包含构建配置，不含已编译镜像。执行以下步骤前必须将本版源码上传/复制到服务器。当前远端 main 仍是此前 v1.2.0-db，不能只 `git pull` 就假定取得了本版 Dockerfile。** 不覆盖原 `.git`、已有 `.env`、`secrets/` 或数据目录。

## 1. 先用受保护的本机端口联调

推荐先保持全部默认值，用 SSH 隧道打开后台，不急着开放公网管理端口。浏览器访问原始 TCP 端口没有意义；9000 给视觉软件，9080 给网页。

确认现有工具：

```bash
docker version
docker compose version
uname -m
cat /etc/os-release
```

本包不自动安装 Docker、不改防火墙、不写入你现有 Nginx 配置。Docker 构建时需要访问基础镜像仓库、发行版软件源及 Python 包源；运行页面的 CSS/JS 不依赖外部 CDN。

### 1.1 准备目录（仅首次新安装）

进入包含 Dockerfile 的项目根目录：

```bash
cd /你的代码路径/AI-vision-tcp-server
# 仅首次执行，已有 .env 不要覆盖：
cp -n .env.example .env
mkdir -p secrets
# 默认 Compose 的宿主机数据路径：
sudo install -d -o 10001 -g 10001 -m 750 /docker_volume/AI-vision-tcp-server/data
```

`install -d` 示例用于新安装；已有比赛数据时先按下文备份，确认属主和权限，不要对共享目录盲目递归 chown。容器使用 UID/GID `10001:10001`，需要能写数据目录。实际路径可在 `.env` 修改 `DATA_PATH`。不要把 SQLite 放在跨机器网络共享文件系统。

### 1.2 构建本地镜像

```bash
docker compose build --pull
```

生成的 `ai-vision-tcp-server:1.3.0-web` 是本机镜像标签，并非已发布到 Docker Hub/GHCR。Dockerfile 没有固定 `--platform`，在 ARM64 或 AMD64 上会按本机架构构建；实际成功与否以本次构建日志和运行测试为准。

### 1.3 创建管理员（没有默认密码）

使用刚构建的镜像交互输入密码，不把密码放进命令行、`.env` 或聊天中：

```bash
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/secrets,dst=/setup" \
  --entrypoint python \
  ai-vision-tcp-server:1.3.0-web \
  tools/init_web_admin.py --username admin --output /setup/admin.json

sudo chown 10001:10001 secrets/admin.json
sudo chmod 600 secrets/admin.json
```

输入两次至少 12 字符的密码。生成文件保存随机盐与 PBKDF2 密码哈希，不保存明文。文件已存在时工具拒绝覆盖。该文件被 `.gitignore` / `.dockerignore` 排除并只读挂载；别把它放进源码包。

Compose 使用 `create_host_path: false`：缺少管理员文件或数据目录会直接报错，不会悄悄生成错误的空目录。

### 1.4 启动

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100 app
```

成功后应有健康状态。`/healthz` 仅检查服务/快照线程/致命故障，不等同于数据库全面检查、参赛端就绪或比赛验收通过。通过后台“完整性检查”进一步确认数据库。

## 2. Windows 电脑通过 SSH 隧道使用

在本地 PowerShell 执行，替换 SSH 用户及服务器地址：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 9080:127.0.0.1:9080 -L 9000:127.0.0.1:9000 SSH用户名@Oracle服务器地址
```

保持此终端开启，然后打开：

```text
裁判后台：http://localhost:9080/admin/
观众大屏：http://localhost:9080/board/
视觉客户端：127.0.0.1:9000
```

默认 `WEB_PUBLIC_URL=http://localhost:9080`，浏览器必须使用 `localhost`，不要换为 `127.0.0.1`、服务器 IP 或其他域名。服务端严格核验 Host 和写操作 Origin，匹配失败不是登录密码错误。

SSH 隧道端口从这台本地电脑可用；另一个比赛电脑/大屏电脑需自己的隧道、VPN，或者后续 HTTPS 部署。不要为了让其他电脑访问就随意让整个裁判桌面镜像到大屏。

## 3. 现场最小操作闭环

登录后台 → 新建“练习”场次 → 选择包装箱或螺钉 → 包装箱设置指定类型 → 点“查看接入码” → 原模拟客户端连接并确认目标 → 选择预期结果和样件类别 → 开始本轮 → 查看实际结果/核对 → 在 `/board/` 看公开结果。

原模拟客户端不猜裁判答案，默认交替发送 OK/NG，测试时按实际序列设置预期。用新场次做正式比赛，不能把练习场次改为正式以混合统计。默认 0 毫秒只记录服务端观测耗时，不判超时；`arm` 是等待外部触发，不等同于相机已经采集。

云端观测耗时包含网络过程，不是算法净耗时。正式比赛须明确统一计时起点、通信路径与允许的时限。

## 4. 改为域名 HTTPS

使用专用子域名，服务位于域名根目录，不部署到任意二级路径。将 `.env` 改为实际地址，例如：

```dotenv
WEB_PUBLIC_URL=https://vision.example.com
WEB_BIND_IP=127.0.0.1
WEB_PUBLISHED_PORT=9080
TCP_BIND_IP=127.0.0.1
TCP_PUBLISHED_PORT=9000
WEB_ALLOW_INSECURE_HTTP=0
```

配置服务器宿主机上的 Nginx，参考 `deploy/nginx.example.conf`，替换域名和已经准备好的证书路径。它不是自动签证书脚本，也不应替换你整个 Nginx 主配置。先验证 Nginx 配置，再按你服务器现有管理方式加载。更改 `.env` 后：

```bash
docker compose up -d --force-recreate
```

后台使用 `/admin/`，大屏使用 `/board/`。HTTPS 配置下 Cookie 启用 Secure。反代必须传入原始 Host。应用不信任 X-Forwarded-For/Proto 来判断身份；当前登录限流在反代后会对共享代理 IP 合并计算，另有全局阈值，适合少量裁判使用。

如果 Nginx 也运行在容器里，里面的 `127.0.0.1` 不是宿主机；让两个容器加入私有网络，反代 `app:9080`，不要直接照抄宿主机代理地址。默认 Compose 未集成你的现有代理网络，需要按实际环境衔接。

Oracle 安全列表/NSG 与主机防火墙只开放必需来源和端口。HTTPS 示例对外需要 443，80 仅按重定向/证书方案决定；默认应用的 9080/9000 不应直接向公网发布。Docker 端口发布与主机防火墙路径有差异，不要只根据 UFW 表面规则判断隔离结果。

**HTTPS 不保护普通 TCP 9000。** 保持参赛连接经过 SSH/VPN 或可信隔离网；本版没有增加 TCP TLS。仅有接入码不代表明文通道已经加密。

## 5. 数据、备份与升级

默认目录：

```text
/docker_volume/AI-vision-tcp-server/data/
├── competition.sqlite3       # 主库
├── competition.sqlite3-wal   # 运行期间可能存在
├── competition.sqlite3-shm   # 运行期间可能存在
├── backups/                 # 已完成的一致性备份目录
├── exports/                 # 场次导出
└── downloads/               # 手动后台任务的下载ZIP
代码目录/secrets/admin.json   # 登录密码哈希，不在数据库备份内
```

在线备份使用 SQLite backup 接口，不是复制正在写入的主 `.sqlite3` 文件。备份与导出 ZIP 都包含裁判私有信息，只给授权人员。数据库、ZIP、密码哈希文件均未实施磁盘加密；需要云盘/系统层面加密和访问控制时应另行配置。

可在后台发起备份并等待任务明确成功，再下载 ZIP。CLI 示例：

```bash
docker compose exec app python database_tools.py --data-dir /data check
docker compose exec app python database_tools.py --data-dir /data backup
```

切场与结束场次自动排队备份，正常退出尽力再备份一次。备份队列最多 8 项；任务显示失败时检查日志和磁盘，不要认为自动备份一定成功。维护任务列表不持久化；重启后列表清空，已完成的文件仍在数据目录。没有自动删除证据的保留策略，需人工归档并监控磁盘。

升级流程：结束/取消当前轮 → 在线备份成功并转存 → `docker compose stop` → 更新源码（保留 `.env`、凭据和数据）→ `docker compose build --pull` → `docker compose up -d` → 登录复核历史与一次练习。

不要使用 `docker compose down -v` 作为常规更新方式，也不要直接删除宿主机数据目录。若维护队列很慢、超出 120 秒停止宽限，容器可能被强制终止；大型导出时先等维护完成再停服。已提交记录依赖 SQLite 原有持久化，下一次启动会将未完成旧轮次标为中断；不重新计时、不补 NG。

从 Windows 数据迁移时，用原程序生成的一致性备份恢复到服务器新的空数据目录，保留旧目录；迁移期间不要让两个实例共享写入。恢复命令和校验规则见 `DATABASE_GUIDE.md`。恢复不在网页里提供，避免在线覆盖正在使用的证据库。

SQLite 运行时警告必须在正式使用前核验。发行版可能通过补丁回移修复而不更新主版本；没有确认来源时不要自行忽略告警。每次更新镜像检查运行时、迁移/备份和实际联调结果。构建使用可更新的基础镜像标签，不是已经固定摘要的长期可复现发行版。

## 6. 重置管理员

管理员文件启动时读取。登录会话和操作回执保存在内存，服务重启后全部重新登录。重置步骤：生成不同名字的新文件 → 停服 → 保留旧文件的受保护副本 → 原子替换 `secrets/admin.json` → 设置 10001:10001 / 600 → 重建启动容器。不要在网页提供任意文件路径和覆盖数据库的接口。

使用命令行参数只指定用户名/输出路径，不要指定明文密码。没有默认密码、找回邮件或多账户系统。

## 7. 单实例与资源边界

一个进程拥有一个比赛引擎和一个当前客户端。不可设置多个 Uvicorn worker、开启 reload、Compose 横向扩容或多个容器挂同一数据库写入。数据库 DataLock 会阻止重复实例，但不要把它作为日常部署协调机制。

默认容器内存限制 768MiB、PID 128、根文件系统只读，只有 `/data` 和 `/tmp` 可写。容器健康不表示客户端已认证；必须在后台看到“参赛端已就绪”。日志滚动保存，实际证据以数据库和完成的导出为准。

ARM64 / AMD64 采用本机构建设计，本次测试环境没有 Docker，未实际构建或运行这两个架构镜像。可在配好的 Buildx 上用多平台构建和镜像仓库分发，但本版没有自动发布流水线。建议先在你的 Oracle 上本地构建，不必先搭建额外数据库、远程桌面或镜像仓库。

## 参考

- Docker bind mounts：https://docs.docker.com/engine/storage/bind-mounts/
- Docker multi-platform：https://docs.docker.com/build/building/multi-platform/
- Docker firewall behavior：https://docs.docker.com/engine/network/packet-filtering-firewalls/
- FastAPI 生命周期：https://fastapi.tiangolo.com/advanced/events/
- SQLite WAL：https://sqlite.org/wal.html
