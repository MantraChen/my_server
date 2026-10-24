# Dev Blog：可选博客组件

将 [MantraChen/Dev-Blog](https://github.com/MantraChen/Dev-Blog) 接入现有 `my_server` 的 CI → 审核合并 → 按组件部署流程。组件名为 `devblog`，默认关闭。

## 接入边界

- `source.json` 固定上游完整 commit；本次是 `bea1a01569b312ad1381497c1970fb809ea25594`。不跟随上游 main 自动更新，也不复制一份博客源码到服务器仓库。更新此 pin 仍须 PR、CI 和仓库所有者审核。
- 保留 `https://howielog.com`、原文章 slug、管理功能、Giscus 和 SQLite。应用有根路径链接和固定 canonical，因此不能直接挂到 `dyyjs.com/blog/`；若另选域名或子路径，应先修改并验证上游应用。
- Linux + systemd + 现有 Caddy；专用 Node 24.22.1 或同主版本后续补丁。不修改其他组件正在使用的 `/opt/dyyjs-youtube/node24-runtime`。
- 监听 `127.0.0.1:4321`，数据在 `/var/lib/dyyjs-devblog`，代码在 `/opt/dyyjs-devblog/releases/<my_server commit>`。HTTP 健康检查返回上游博客 commit；部署台账记录服务器仓库 commit，两者不是同一个 SHA。
- 不启用 Windows webhook，不自动改 DNS、共享 Caddyfile 或其他服务，不自动创建空博客数据库。服务器规格、系统版本、实际负载和备份恢复仍须在目标机确认。

## 首次准备（服务器所有者执行）

先确认 Git、Python 3.10+、systemd、npm、原生模块编译工具（如 `make`/C++ 编译器）可用，4321 未占用，至少 2 GiB 可用磁盘。安装及构建峰值内存需实测；构建把 Node 堆上限设为 768 MiB，这不是整个进程树的内存硬限制。构建依赖沿用仓库的回环代理 `http://127.0.0.1:7890`。

安装独立的官方 Node 24 运行时到 `/opt/dyyjs-devblog/node`（含 `bin/node` 和 npm），按机器架构下载并校验官方 `SHASUMS256.txt`。目录及父目录必须 root 所有、不可由组或其他用户写入。不要把 Windows 的 node_modules 拷到 Linux。版本要求由 `check-runtime.mjs` 和上游安装检查共同验证。

下面只创建账户及空目录；已有同名账户须先核验 UID、组和用途。尚未创建 `enabled` 文件时，合并本 PR 只会安装新版总部署器，`devblog` 会跳过且不写入成功台账。

```sh
sudo useradd --system --user-group --home-dir /var/lib/dyyjs-devblog --shell /usr/sbin/nologin devblog
sudo useradd --system --user-group --home-dir /var/cache/dyyjs-devblog-build --shell /usr/sbin/nologin devblog-build
sudo install -d -o root -g root -m 0755 /opt/dyyjs-devblog /opt/dyyjs-devblog/releases /opt/dyyjs-devblog/incoming
sudo install -d -o root -g root -m 0700 /etc/dyyjs-devblog
sudo install -d -o devblog -g devblog -m 0700 /var/lib/dyyjs-devblog /var/lib/dyyjs-devblog/backups
sudo install -d -o devblog-build -g devblog-build -m 0700 /var/cache/dyyjs-devblog-build
```

用 `.env.example` 创建 `/etc/dyyjs-devblog/env`，属主 root、权限 0600。只允许 `ADMIN_PASSWORD_HASH=<现有 bcrypt hash>` 一行，值不加引号；不要设置 DATABASE_PATH/HOST/NODE_OPTIONS。真实 hash 不应进入 Git、Actions 变量或命令历史。systemd 的 EnvironmentFile 不执行 shell 变量展开。

将现有博客数据库的**一致备份副本**导入尚为空的 `/var/lib/dyyjs-devblog/data.db`，属主 `devblog:devblog`、权限 0600。SQLite WAL 模式下不能只复制正在写入的 `data.db`；使用 SQLite backup API。先用演练副本跑通，再在切换窗口暂停旧站所有写入、旧 webhook；同时停止新机的演练服务并关闭所有 SQLite 连接，保留演练库及其 sidecar 后再导入最终一致备份。不要覆盖正在使用的数据库，也不要对已承接真实写入的新库重复执行导入。迁移本身及统计汇总的兼容限制见上游 `docs/REVIEW.md`。

检查完毕后显式启用组件：

```sh
sudo install -o root -g root -m 0600 /dev/null /etc/dyyjs-devblog/enabled
```

在现有 Deploy workflow 中由仓库所有者选择 `operation=deploy`、已通过 `CI / required` 的完整 `my_server` SHA，`components=devblog`。首次合并后应先确认 deployment 组件版本 7 已安装。也可在服务器使用同一入口：

```sh
sudo /usr/local/sbin/deploy-my-server deploy <my_server-full-sha> devblog
```

删去 `enabled` 只暂停后续部署，不停止已运行的博客；需要停站时显式 `systemctl stop dyyjs-devblog`。

## 部署、验收与回退

1. 旧服务继续运行时，以 `devblog-build` 获取固定源码，在新目录执行 `npm ci`、完整 `npm run verify` 和全依赖审计。构建不接收生产密码或数据库路径。
2. 固化为 root 所有的 release；使用数据库一致副本，以 `devblog` 账户演练迁移并检查完整性。失败不会停止旧服务。
3. 停止旧服务，创建并验证独立备份，再迁移持久数据库；切换 current 和 systemd unit，启动并核对回环健康接口的精确上游 commit。成功才启用开机启动并更新外层部署台账。
4. 迁移、启动或健康检查失败会恢复原 current、unit 和运行状态。总部署器也快照代码链接和 unit，处理后续其他组件失败；**两层回退都不覆盖数据库**。数据库备份在持久目录中保留，需制定异机备份和保留策略，避免长期累积占满磁盘。

在现有 Caddyfile 中**追加** `Caddyfile.example` 的独立站点段，保留已有全局选项和 dyyjs.com 路由；先 `caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile`，再 reload。此操作需人工执行，部署器不重载共享代理。该片段假设用户直接连接 Caddy；若前面另有 CDN/代理，须单独验证可信代理配置及真实 IP 限流。只对公网开放已有 80/443，4321 保持回环监听。

正式改 DNS 前在新机或测试客户端使用域名解析覆盖验证 HTTPS、登录、搜索、文章阅读、评论配置、反应统计、旧链接、RSS/sitemap 与备份恢复。证书签发条件需由服务器所有者安排；不得用跳过 TLS 验证当作上线验收。应用完整验收后再切换 DNS，期间避免两套站点同时写入。

已部署的服务器仓库 SHA 可通过现有 `rollback <sha> devblog` 回到其**已验证且仍保留在本机**的博客 release。这不是数据库降级：仅对兼容当前 schema 的旧代码使用。新的破坏性迁移必须另行设计恢复方案。上线后旧机已落后的数据库不能直接重新启用；如需切回旧机，先停止新机写入并迁移最新一致备份。

## 验证范围

本地部署控制测试覆盖源码锁定、env 限制、WAL 一致备份、副本迁移、构建/演练失败不停止旧服务、迁移/启动/健康失败恢复服务、数据库新写入保留、初次失败清理、hardlink 快照兼容及健康版本核对。Linux CI 另外运行固定上游的完整检查、构建和真实 HTTP 冒烟测试；`tests/smoke-proxy.mjs` 验证 Caddy 转发头对应的 HTTPS 登录、Secure Cookie、CSRF 和精确版本健康检查。

本次准备发生在 Windows 上，systemctl/runuser 权限及 Linux symlink 切换仍须 Linux CI 和目标机演练；模拟控制流程测试不代表已经完成生产部署。

参考：[systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)、[Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。
