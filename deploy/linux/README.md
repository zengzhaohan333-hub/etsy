# Linux 独立运行

推荐 Ubuntu 24.04 LTS、Python 3.11 以上、systemd 247 以上。程序只支持欧易模拟盘，不提供实盘开关。服务器需要稳定访问欧易官方 HTTPS 接口；不会沿用电脑上的 Windows 本地代理地址。安装后也必须先完成只读检查，再由你明确启动服务。

这些是待安装的部署文件，并不表示已经有可用服务器或已经完成部署。请先确认有能通过 SSH 登录的独立 Linux 服务器；Windows 电脑里的 Ubuntu/WSL 不属于此部署目标，安装脚本会在任何部署修改前拒绝 WSL。没有独立服务器时，不要在当前电脑运行安装器。Python 3.10 不满足要求；脚本不会替你升级操作系统或修改 WSL。

代码保存在 `/opt/okxbot/app`，虚拟环境在 `/opt/okxbot/venv`，非密钥配置在 `/etc/okxbot/config.portfolio.toml`。持久订单日志、状态和风控数据保存在 `/var/lib/okxbot`，不放进 Git。程序及配置由 root 管理，服务使用没有登录权限的 `okxbot` 用户。

## 安装

先通过 SSH 登录你自己的服务器并下载整个项目。不要在聊天中发送 SSH 密码、私钥或 API 密钥。若服务器缺少基础工具，可自行安装发行版的 `python3`、`python3-venv`、`util-linux`；安装脚本不会运行 apt、下载软件或更改防火墙。

在项目目录运行：

```sh
sudo bash deploy/linux/install.sh --source "$PWD"
```

脚本使用不含 pip 的干净虚拟环境，项目没有第三方运行依赖。它部署受控源文件，并在虚拟环境中登记根用户拥有的只读源码路径，方便从任意目录执行 `python -m`。它不会启动或启用服务，不会获取密钥，不会覆盖已有策略配置和持久状态。

升级前先停止两个服务，并核对欧易模拟盘的仓位及止盈止损单。已有应用目录会保留为备份。若已安装的 unit 与新版本不同，脚本会停止；审核差异后可显式使用 `--replace-units`，旧 unit 会备份。不要直接覆盖尚未核对的恢复日志。

## 从 Windows 迁移

先停止原 Windows 机器人，并防止其再次自动启动。同一 API 在不同机器上的本地文件锁不能相互阻止，两边同时运行会有重复交易风险。

在欧易确认四币仓位、普通挂单和策略单的实际状态。保留原 `.runtime-demo/demo-journal.json`；若存在 `.runtime-demo/session.json`，必须一起原样保留和迁移。后者保存会话权益高点、回撤熔断及账户/配置绑定，不能重建或改写来获得新的风险额度。若旧程序仍标记待核对，应先使用原恢复入口完成核对，不能删除或新建空日志来绕过检查。

将原状态目录中的这两个文件安全传到服务器同一目录。确认服务器交易服务已停止后，首次迁移可用下面的命令；它会在任一目标文件已存在时拒绝覆盖，并将复制的文件设为 `okxbot` 拥有、权限 0600：

```sh
sudo bash -c '
set -e
for name in demo-journal.json session.json; do
  if test -e "/var/lib/okxbot/$name" || test -L "/var/lib/okxbot/$name"; then
    printf "Existing state retained: %s\n" "$name" >&2
    exit 1
  fi
done
test -f "$1/demo-journal.json"
install -o okxbot -g okxbot -m 0600 "$1/demo-journal.json" /var/lib/okxbot/demo-journal.json
if test -f "$1/session.json"; then
  install -o okxbot -g okxbot -m 0600 "$1/session.json" /var/lib/okxbot/session.json
fi
' bash "/你上传的状态目录"
```

仅在目标文件不存在时执行上面的首次迁移；若服务器已有状态，先停止服务并保留双方文件，核对后再决定迁移方式。最终位置分别为 `/var/lib/okxbot/demo-journal.json` 和 `/var/lib/okxbot/session.json`。旧版本原本没有 `session.json` 时，不要手工创建空文件；应保留原订单日志，按下文进行显式首次初始化。正常交易服务绝不会自动补建缺失的风控文件。安装脚本不会替你迁移、合并、初始化会话或解除日志保护。

## 在服务器输入模拟盘凭证

使用专用模拟账户创建的 API，并保留最小必要权限。源凭证保存在 `/etc/okxbot/credentials`，目录权限 0700，三个文件权限 0600；它们不在源码目录。systemd 通过 `LoadCredential` 将只读副本交给服务，凭证值不会出现在 unit、命令参数或环境变量中。

首次初始化前，请先在欧易模拟盘将 BTC、ETH、SOL、XRP 四个 USDT 永续合约设为逐仓、1 倍杠杆，并确认四币空仓且无挂单。后台服务与会话初始化只核对这些设置，服务重启不会替你修改杠杆；设置不匹配会以安全停止码 78 拒绝启动。

在 SSH 交互终端中执行，按照隐藏输入提示填写：

```sh
sudo /opt/okxbot/venv/bin/python -I -m okx_bot.service_credentials --directory /etc/okxbot/credentials
```

不要把密钥写在 shell 命令、`.env`、Git 文件或聊天里。需要更换凭证时先停止机器人，核对模拟盘状态，再明确使用配置工具的 `--replace`。

## 检查与启动

首次部署先进行只读检查，当前四币应为空仓且无挂单；此命令不启动自动交易。使用临时 systemd 任务，以同一个 `okxbot` 用户运行，并通过系统私有凭证副本读取密钥：

```sh
sudo systemd-run --unit=okxbot-check --wait --collect --pipe --uid=okxbot --gid=okxbot \
  --property=WorkingDirectory=/opt/okxbot/app --property=PrivateTmp=false --property=UMask=0077 \
  --property=LoadCredential=api_key:/etc/okxbot/credentials/api_key \
  --property=LoadCredential=api_secret:/etc/okxbot/credentials/api_secret \
  --property=LoadCredential=api_passphrase:/etc/okxbot/credentials/api_passphrase \
  /opt/okxbot/venv/bin/python -m okx_bot.service --config /etc/okxbot/config.portfolio.toml \
  --state-dir /var/lib/okxbot --check-only
```

### 显式初始化首次会话

如果原来已经存在并迁移了 `session.json`，跳过本段，不要重置或重新初始化。只有确认这是从未创建过会话的新部署，或者旧版本确实从未有过该文件，才能在原 Windows 机器人和服务器交易服务都已停止、四币已核实空仓无挂单、原订单日志已迁移且全部结束后，执行下面的首次初始化。若风险文件曾经存在但现在丢失，应先恢复可靠备份，不能借初始化获得新的风险额度。

初始化会持有同一 API 和状态目录的锁，再核对账户与订单日志。它不启动交易，不会覆盖现有风险文件或清除已锁定的回撤熔断：

```sh
sudo systemctl stop okxbot.service
sudo systemd-run --unit=okxbot-initialize --wait --collect --pipe --uid=okxbot --gid=okxbot \
  --property=WorkingDirectory=/opt/okxbot/app --property=PrivateTmp=false --property=UMask=0077 \
  --property=LoadCredential=api_key:/etc/okxbot/credentials/api_key \
  --property=LoadCredential=api_secret:/etc/okxbot/credentials/api_secret \
  --property=LoadCredential=api_passphrase:/etc/okxbot/credentials/api_passphrase \
  /opt/okxbot/venv/bin/python -m okx_bot.service --config /etc/okxbot/config.portfolio.toml \
  --state-dir /var/lib/okxbot --initialize-session --ack-session-init
```

正常服务必须同时具有与账户、配置匹配的原订单日志和会话风控文件。已有 `session.json` 却缺失 `demo-journal.json` 会持续阻止交易，应恢复并核对原配对文件，不能新建空日志绕过。

### 启动服务

只读检查通过、原 Windows 机器人已停止，且原日志及已有会话文件已正确迁移（首次会话已按上文明确初始化）后，再明确启动：

```sh
sudo systemctl start okxbot.service
sudo systemctl status okxbot.service --no-pager
sudo journalctl -u okxbot.service -n 50 --no-pager
```

确认运行正常后，按需要启用开机启动：

```sh
sudo systemctl enable okxbot.service
```

短暂网络故障由控制器暂停新开仓、只读重试和核对；系统服务不会把未知订单当成可重试下单。退出码 1、70、78 会阻止自动重启，分别保留兼容错误、程序异常或安全/认证/风控/日志停止；75 仅供已判定安全的临时启动故障有限重试。十分钟内最多启动三次。被保护停止时应核对交易所和本地日志，不能使用循环重启或删除日志解决。

停止：

```sh
sudo systemctl stop okxbot.service
```

停止程序不会自动平掉已有模拟持仓。请在欧易确认剩余仓位和保护单。服务没有启用 `PrivateTmp`，因为当前同机同 API 的并发锁需要共享 `/tmp`；不要用另一个 Linux 用户或容器绕过该锁。

## 核对已结束的订单日志

若服务因旧订单日志阻止启动，先停止 `okxbot.service`，并在欧易确认 BTC、ETH、SOL、XRP 均为空仓，且没有普通挂单或策略单。保留原日志，在服务器运行下面的恢复核对；它只查询交易所，并且只有全部订单已核实结束时才更新本地日志，不会下单、撤单或启动自动交易：

```sh
sudo systemctl stop okxbot.service
sudo systemd-run --unit=okxbot-reconcile --wait --collect --pipe --uid=okxbot --gid=okxbot \
  --property=WorkingDirectory=/opt/okxbot/app --property=PrivateTmp=false --property=UMask=0077 \
  --property=LoadCredential=api_key:/etc/okxbot/credentials/api_key \
  --property=LoadCredential=api_secret:/etc/okxbot/credentials/api_secret \
  --property=LoadCredential=api_passphrase:/etc/okxbot/credentials/api_passphrase \
  /opt/okxbot/venv/bin/python -m okx_bot.service --config /etc/okxbot/config.portfolio.toml \
  --state-dir /var/lib/okxbot --reconcile --ack-journal
```

恢复任务与交易服务使用同一用户，避免生成 root 独占的日志或账户锁。不要直接以 root 运行会改写日志的恢复命令。核对失败时保留保护状态，不要删除日志；核对成功也不会重置 `session.json` 或解除回撤熔断。需要继续运行时，在确认问题已解决后单独执行 `sudo systemctl start okxbot.service`。

## 查看监控

监控服务只读取状态文件，不需要 API 凭证，不启动机器人：

```sh
sudo systemctl start okxbot-monitor.service
```

它只监听服务器的 `127.0.0.1:8766`。在自己的电脑建立 SSH 隧道：

```sh
ssh -L 8876:127.0.0.1:8766 你的SSH用户@服务器地址
```

保持隧道窗口开启，再在本机浏览器访问 `http://127.0.0.1:8876`；本机使用 8876，避免与旧 WSL 监控的 8766 冲突。不要开放服务器公网 8766 端口，不要将当前未提供公网身份认证的面板直接暴露给互联网。关闭网页或监控服务不会停止交易服务。

历史回测和模拟运行均不保证盈利；手续费、滑点、资金费及真实成交条件会影响结果。
