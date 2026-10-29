# Windows 上第一次使用（WSL）

先启动本地模拟面板。这一步不需要欧易账户、API 密钥或真实资金。价格是合成演示数据，只用于检查程序，不能当作收益回测。

## 1. 下载并解压

把运行包解压到 Windows 的「下载」文件夹，得到完整的 `okx-bot` 文件夹。打开后应能看到 `start-windows.bat`、`README.md`、`config.paper.toml` 和 `okx_bot` 子文件夹。不要直接在压缩包里运行，也不用把文件复制到 Linux 的主目录。

程序需要 Python **3.11 或更高版本**，没有第三方运行依赖，不需要安装交易 SDK。

## 2. 准备 WSL 和 Ubuntu

当前版本依赖 Unix 文件锁，由启动器通过 **WSL 的 Ubuntu**运行；不需要安装 Windows 版 Python。

如果尚未安装 WSL 和 Ubuntu 24.04，在管理员 PowerShell 中运行：

```powershell
wsl --install -d Ubuntu-24.04
```

按系统提示安装、必要时重启，从开始菜单打开 Ubuntu 24.04，首次运行时设置本机 Linux 用户名和密码。

启动器使用 WSL 的默认发行版。在 PowerShell 中将它设为 Ubuntu 24.04：

```powershell
wsl --set-default Ubuntu-24.04
```

这会改变 WSL 的默认发行版；如果你原来使用其他发行版，请留意此变化。

在 Ubuntu 24.04 终端中检查 Python：

```bash
python3 --version
```

确认 Python 版本不低于 3.11。若 Ubuntu 尚未安装 Python，可使用系统包管理器：

```bash
sudo apt update
sudo apt install python3
```

完成后再次检查版本。以上 WSL 安装步骤需要在你的电脑上执行；云端测试不代表已经验证你的 Windows 环境。

## 3. 打开本地模拟面板

回到 Windows 的「下载」文件夹，打开解压后的 `okx-bot`，双击 **`start-windows.bat`**。

看到「本地模拟面板已启动」后，保持终端打开，在**同一台电脑**的浏览器地址栏输入：

```text
http://127.0.0.1:8765
```

这是电脑上的本地地址，不是云端预览链接。Windows 使用 WSL 时，也在 Windows 浏览器中打开此地址。

点击「开始模拟」，应能看到价格图、账户权益和运行事件；成交记录会在策略触发并平仓后出现。约半分钟完成一轮。每轮从虚拟初始余额重新开始。

点击「停止模拟」结束这一轮；在启动窗口中按 **Ctrl+C** 关闭面板服务。使用期间保持启动窗口打开；关闭电脑后程序不会继续运行。

如果窗口提示 Python、WSL 或端口错误，保留错误文字，先解决启动问题。不要同时重复启动多个窗口。

## 4. 面板跑通后，再接欧易模拟盘

本地电脑运行**不需要**发布聊天中的云环境配置；那份网络放行配置只影响云端机器。接交易所时，你的电脑需要能够正常访问 `www.okx.com`。

在欧易的「模拟交易」账户创建专用 API，只开**读取、交易**权限。通过系统密钥管理器或安全的环境变量配置 `OKX_API_KEY`、`OKX_API_SECRET`、`OKX_API_PASSPHRASE`。不要把密钥写进终端命令历史、源码、聊天或截图；本程序不会自动读取 `.env`。

凭证准备好后，先进行只读连接检查，再决定启动模拟盘订单。账户模式、命令和故障恢复步骤见 [README.md](README.md) 的「接入 OKX 模拟盘」与「订单与故障处理」；在自己的电脑执行命令时，工作目录应是解压后的项目目录，而不是云端的 `/workspace/etsy`。

`start-windows.bat` 和面板只控制本地模拟，不会配置密钥或启动交易所订单。**停止模拟盘程序不会自动平掉交易所持仓。**
