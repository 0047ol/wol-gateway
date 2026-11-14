# WOL Gateway

**界面截图 / Screenshots**

![网关管理桌面界面 / Gateway management on desktop](screenshots/gateway-overview-desktop.png)

![设备列表移动端界面 / Device list on mobile](screenshots/device-list-mobile.png)

面向家庭和小型办公室的 ESP32-C3 局域网设备管理系统。网关通过 Wi-Fi 接入服务端，扫描和监测局域网设备，并提供 Wake-on-LAN 唤醒、Windows 关机服务、用户权限、设备分享和固件远程 OTA。

项目包含 FastAPI 网站与设备接入服务、ESP32-C3 固件、Windows WOL 关机服务，以及离线部署配置向导。部署向导只在浏览器本地生成配置文件，不会上传主机名、密码或密钥。

## 功能

- ESP32-C3 Wi-Fi 配网、网关配对、局域网扫描和在线状态监测。
- 多用户与网关归属、权限控制、审计记录及设备分享。
- WOL 唤醒；可选 Windows 服务提供电脑在线状态和关机能力。
- 固件签名验证、远程 OTA 与失败回滚。
- SQLite 本地开发，MySQL 部署；MQTT 用于网关与服务端通信。

## 目录

| 目录 | 内容 |
| --- | --- |
| app/ | FastAPI 应用、网页模板和静态资源 |
| firmware/ | ESP32-C3 固件源码和构建配置 |
| windows-agent/native/ | Windows WOL 关机服务源码 |
| deploy/setup.html | 离线部署配置向导 |
| tools/package_release.py | 生成脱敏源码发布 ZIP |

预构建固件、Windows EXE、数据库、生产配置、签名私钥和内部运维记录不会进入源码发布 ZIP。OTA 验签公钥不是签名私钥；发布者必须自行保管签名私钥，并配置自己的服务器地址和信任证书。

## 本地运行

需要 Python 3.10+、Node.js（前端测试）和可访问的 MQTT Broker。设置 MQTT 与数据库环境变量后运行：

    py -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    $env:DEBUG = 'false'
    $env:MQTT_HOST = '127.0.0.1'
    $env:MQTT_PORT = '1883'
    .venv\Scripts\python.exe run.py

首次打开 http://127.0.0.1:8088/setup 设置管理员密码。纯本地开发可使用 SQLite；部署前应配置独立数据库账号、MQTT ACL、HTTPS 证书和非调试运行模式。不要将真实配置、数据库、设备绑定密钥、MQTT 密码、TLS 私钥或固件签名私钥提交到版本库。

## 部署配置向导

用浏览器打开 deploy/setup.html。填写部署域名、数据库和 MQTT 参数后，可下载生成的服务端环境变量、MySQL 凭据文件和 systemd 示例。所有输入只保存在当前页面内存，不调用网络接口；生成后请按 deploy/README.md 检查权限、TLS 端口、反向代理和防火墙，再安装服务。向导不会自动登录服务器或执行远程命令。

服务器地址也需要配置到固件和 Windows 服务源码中。源码包会将项目作者使用的服务器地址替换为 YOUR_SERVER_HOST；发布者须在编译前将占位符改为自己的域名，并使用自己的信任证书和 OTA 签名密钥。

## 构建和测试

开发依赖安装后，可运行 Python 单元测试和 Node 测试：

    .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
    .venv\Scripts\python.exe -m unittest discover -s tests -v
    node --test tests/portal_flow.test.cjs tests/gateway_pairing.test.cjs
    .\firmware\build.ps1

Windows 服务可用 windows-agent/native/Build.ps1 构建。真实硬件刷写、服务安装、关机及远程部署均需在目标环境单独验证。

## 许可证

本项目使用 GNU Affero General Public License v3.0 or later，见 LICENSE。项目署名见 NOTICE。保留原作者及版权声明；分发修改版本时，在修改过的文件或随源码发布的变更记录中注明修改内容和日期，并依 AGPL 提供完整对应源码。通过网络向用户提供修改版服务时，也需向这些用户提供对应源码。AGPL 允许商业使用、修改和再分发，但要求遵守源码提供和相同许可证条款。

请另外检查依赖、字体、图标、证书及其他第三方组件各自的许可；本项目许可证不覆盖第三方材料。

## English

WOL Gateway is a self-hosted ESP32-C3 and LAN device management system for homes and small offices. Gateways connect to the server over Wi-Fi to discover and monitor LAN devices. The system provides Wake-on-LAN, an optional Windows shutdown service, multi-user access control, device sharing, signed firmware OTA, and an offline deployment configurator.

The deployment configurator generates server configuration files locally in your browser. It does not upload hostnames, passwords, or keys.

### Screenshots

![Gateway management on desktop](screenshots/gateway-overview-desktop.png)

![Device list on mobile](screenshots/device-list-mobile.png)

### Features

- ESP32-C3 Wi-Fi provisioning, gateway pairing, LAN discovery, and presence monitoring.
- Multiple users, gateway ownership, access control, audit records, and device sharing.
- Wake-on-LAN; the optional Windows service reports PC presence and supports shutdown.
- Signed firmware verification, remote OTA, and rollback after failed updates.
- SQLite for local development, MySQL for deployment, and MQTT for gateway-to-server communication.

### Repository Layout

| Path | Contents |
| --- | --- |
| `app/` | FastAPI application, templates, and static assets |
| `firmware/` | ESP32-C3 firmware source and build configuration |
| `windows-agent/native/` | Windows WOL shutdown service source |
| `deploy/setup.html` | Offline deployment configurator |
| `tools/package_release.py` | Creates a sanitized source archive |

Prebuilt firmware, Windows executables, databases, production configuration, signing private keys, and internal operations records are excluded from the source archive. The OTA verification certificate is public material, not a signing key. Configure your own server address and trust certificate, and keep your signing private key secure.

### Run Locally

Requires Python 3.10+, Node.js for frontend tests, and an accessible MQTT broker. Configure the MQTT and database environment variables, then run:

    py -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    $env:DEBUG = 'false'
    $env:MQTT_HOST = '127.0.0.1'
    $env:MQTT_PORT = '1883'
    .venv\Scripts\python.exe run.py

Open `http://127.0.0.1:8088/setup` on first run to set the administrator password. SQLite can be used for local development. Before deployment, configure a dedicated database account, MQTT ACLs, HTTPS, and non-debug mode. Never commit real configuration, databases, device binding secrets, MQTT passwords, TLS private keys, or firmware signing private keys.

### Deployment Configurator

Open `deploy/setup.html` in a browser. Enter the deployment domain, database, and MQTT settings to download server environment variables, MySQL credentials, and systemd examples. Inputs remain in the current page's memory; the configurator makes no network requests and does not log into or execute commands on a server. Review `deploy/README.md` for file permissions, TLS, reverse proxy, and firewall setup.

The server address must also be configured in the firmware and Windows service source. The source archive replaces the original project server address with `YOUR_SERVER_HOST`. Replace that placeholder before building, and use your own trust certificate and OTA signing key.

### Build and Test

After installing development dependencies, run Python unit tests and Node tests:

    .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
    .venv\Scripts\python.exe -m unittest discover -s tests -v
    node --test tests/portal_flow.test.cjs tests/gateway_pairing.test.cjs
    .\firmware\build.ps1

Build the Windows service with `windows-agent/native/Build.ps1`. Verify hardware flashing, service installation, shutdown, and remote deployment separately in their target environments.

### License

This project is licensed under GNU Affero General Public License v3.0 or later; see `LICENSE`. Attribution details are in `NOTICE`. Retain the original author and copyright notices. When distributing modified versions, add prominent notices to modified files or a change log shipped with the source, including what changed and when. Provide the complete corresponding source under AGPL terms. If you offer a modified version as a network service, provide its corresponding source to those users. Commercial use, modification, and redistribution are permitted subject to the source and license requirements.

Check the licenses of dependencies, fonts, icons, certificates, and other third-party components separately. This project's license does not cover third-party materials.
