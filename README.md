<p align="center">
  <a href="https://github.com/PasarGuard/panel" target="_blank" rel="noopener noreferrer">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://github.com/PasarGuard/PasarGuard.github.io/raw/main/public/logos/PasarGuard-white-logo.png">
      <img width="160" height="160" src="https://github.com/PasarGuard/PasarGuard.github.io/raw/main/public/logos/PasarGuard-black-logo.png">
    </picture>
  </a>
</p>

<h1 align="center">🛡️ PasarGuard</h1>

<p align="center">
    <strong>Unified & Censorship-Resistant Proxy Management Solution</strong>
</p>

---

> **Note:** This is [Free-Guy-IR](https://github.com/Free-Guy-IR)'s fork of the original [PasarGuard panel](https://github.com/PasarGuard/panel), extended with **sing-box (Hysteria2)**, **OpenVPN**, and **MTProto (Telegram proxy)** as additional core types alongside Xray and WireGuard.

---

<br/>
<p align="center">
    <a href="https://github.com/PasarGuard/panel/actions/workflows/build.yml" target="_blank">
        <img src="https://img.shields.io/github/actions/workflow/status/PasarGuard/panel/build.yml?style=flat-square" />
    </a>
    <a href="https://hub.docker.com/r/PasarGuard/panel" target="_blank">
        <img src="https://img.shields.io/docker/pulls/pasarguard/panel?style=flat-square&logo=docker" />
    </a>
    <a href="https://github.com/PasarGuard/panel/blob/main/LICENSE" target="_blank">
        <img src="https://img.shields.io/github/license/PasarGuard/panel?style=flat-square" />
    </a>
    <a href="https://t.me/Pasar_Guard" target="_blank">
        <img src="https://img.shields.io/badge/telegram-group-blue?style=flat-square&logo=telegram" />
    </a>
    <a href="https://github.com/PasarGuard/panel" target="_blank">
        <img src="https://img.shields.io/github/stars/PasarGuard/panel?style=social" />
    </a>
</p>

<p align="center">
 <a href="./README-fa.md">
 🇮🇷 فارسی
 </a>
  /
  <a href="./README-zh-cn.md">
 🇨🇳 简体中文
 </a>
   /
  <a href="./README-ru.md">
 🇷🇺 Русский
 </a>
</p>

<p align="center">
  <a href="https://github.com/PasarGuard/panel" target="_blank" rel="noopener noreferrer" >
    <img src="https://github.com/PasarGuard/PasarGuard.github.io/raw/main/public/logos/screenshot.png" alt="PasarGuard screenshots" width="600" height="auto">
  </a>
</p>

## 📋 Table of Contents

> **Quick Navigation** - Jump to any section below

-   [📖 Overview](#-overview)
    -   [🤔 Why using PasarGuard?](#-why-using-pasarguard)
        -   [✨ Features](#-features)
-   [🚀 Installation guide](#-installation-guide)
    -   [🖥️ Multiple Nodes on One Server](#️-multiple-nodes-on-one-server)
-   [📚 Documentation](#-documentation)
-   [💖 Donation](#-donation)

---

# 📖 Overview

> **What is PasarGuard?**

PasarGuard is a powerful proxy management tool that offers an intuitive and efficient interface for handling hundreds of proxy accounts. Built with Python and React.js it combines performance, scalability, and ease of use to simplify large-scale proxy management. It supports [Xray-core](https://github.com/XTLS/Xray-core), [WireGuard](https://www.wireguard.com/), [sing-box (Hysteria2)](https://github.com/SagerNet/sing-box), OpenVPN, and MTProto (Telegram proxy) for maximum performance and flexibility.

---

## 🤔 Why using PasarGuard?

> **Simple, Powerful, Reliable**

PasarGuard is a user-friendly, feature-rich, and reliable proxy management tool. It allows you to create and manage multiple proxies for your users without the need for complex configuration. With its built-in web interface, you can easily monitor activity, modify settings, and control user access limits — all from one convenient dashboard.

---

### ✨ Features

<div align="left">

**🌐 Web Interface & API**
- Built-in **Web UI** dashboard
- Fully **REST API** backend
- **Multi-Node** support for infrastructure distribution

**🔐 Protocols & Security**
- Supports **Vmess**, **VLESS**, **Trojan**, **Shadowsocks**, **WireGuard**, **Hysteria2**, **OpenVPN** and **MTProto** (Telegram proxy)
- **TLS** and **REALITY** support
- **Multi-protocol** for a single user

**👥 User Management**
- **Multi-user** on a single inbound
- **Multi-inbound** on a **single port** (fallbacks support)
- **Traffic** and **expiry date** limitations
- **Periodic** traffic limit (daily, weekly, etc.)
- **HWID/device limits** for hardware-bound access control

**🔗 Subscriptions & Sharing**
- **Subscription link** compatible with **V2ray**, **Clash** and **ClashMeta**
- Automated **Share link** and **QRcode** generator
- System monitoring and **traffic statistics**

**🛠️ Tools & Customization**
- Customizable xray configuration
- Integrated **Telegram Bot**
- **Command Line Interface (CLI)**
- **Multi-language** support
- **Multi-admin** support with **RBAC** for granular permissions and scoped access

</div>

---

# 🚀 Installation guide

> **Quick Start** - Get PasarGuard running in minutes

### For a quick setup, use the following commands based on your preferred database.

---

**TimescaleDB (Recommended):**
```bash
sudo bash -c "$(curl -fsSL https://github.com/Free-Guy-IR/scripts/raw/main/pasarguard.sh)" @ install --database timescaledb
```

**SQLite:**
```bash
sudo bash -c "$(curl -fsSL https://github.com/Free-Guy-IR/scripts/raw/main/pasarguard.sh)" @ install
```

**MySQL:**
```bash
sudo bash -c "$(curl -fsSL https://github.com/Free-Guy-IR/scripts/raw/main/pasarguard.sh)" @ install --database mysql
```

**MariaDB:**
```bash
sudo bash -c "$(curl -fsSL https://github.com/Free-Guy-IR/scripts/raw/main/pasarguard.sh)" @ install --database mariadb
```

**PostgreSQL:**
```bash
sudo bash -c "$(curl -fsSL https://github.com/Free-Guy-IR/scripts/raw/main/pasarguard.sh)" @ install --database postgresql
```

### 📋 After installation:

<div align="left">

**📋 Watch the logs** (press `Ctrl+C` to stop)

**📁 Files are located at** `/opt/pasarguard`

**⚙️ Config file:** `/opt/pasarguard/.env` (see [Configuration](https://docs.pasarguard.org/en/panel/configuration) for details)

**💾 Data files:** `/var/lib/pasarguard`

**🔒 Important:** Dashboard requires SSL certificate for security
- Get SSL certificate: [Guide](https://docs.pasarguard.org/en/learn/ssl-certificate-cli)
- Access: `https://YOUR_DOMAIN:8000/dashboard/`

**🔗 For testing without domain:** Use SSH port forwarding (see below)

</div>

---

```bash
ssh -L 8000:localhost:8000 user@serverip
```

Then access: `http://localhost:8000/dashboard/`

> ⚠️ **Testing only** - You'll lose access when you close the SSH terminal.

### 🔧 Next Steps:

```bash
# Generate a one-time setup key for owner account setup
pasarguard cli generate-temp-key

# Use the key on the dashboard login page to create the owner account

# Get help
pasarguard --help
```

---

### 🖥️ Multiple Nodes on One Server

If you want to run several nodes on the same server, install each node with a unique name:

```bash
sudo bash -c "$(curl -sL https://github.com/Free-Guy-IR/scripts/raw/main/pg-node.sh)" @ install --name node-eu-1
```

After installation, use that same name as the prefix for management commands:

```bash
node-eu-1 update
node-eu-1 edit
node-eu-1 edit-env
```

You can run any other subcommand for that same node with the same prefix (`node-eu-1 ...`).

> ⚠️ **Important:** Never reuse ports between nodes. The connection port and every core's inbound/instance ports (Xray, sing-box, OpenVPN, MTProto, ...) attached to each node must be completely unique per node.

# 📚 Documentation

<div align="left">

**📖 Official Documentation** - Complete guides available in:

🇺🇸 **[English](https://docs.pasarguard.org/en)**

🇮🇷 **[فارسی](https://docs.pasarguard.org/fa)**

🇷🇺 **[Русский](https://docs.pasarguard.org/ru)**

🇨🇳 **[简体中文](https://docs.pasarguard.org/zh-cn)**

</div>

> **Contributing:** Help improve documentation on [GitHub](https://github.com/PasarGuard/PasarGuard.github.io)

---

# 💖 Donation

<div align="left">

> **Support PasarGuard Development**

If PasarGuard helps you, consider supporting its development:

[![Donate](https://img.shields.io/badge/Donate-Support%20Us-green?style=for-the-badge)](https://donate.pasarguard.org)

**Thank you for your support!** 💖

</div>

---

<p align="center">
  Made with ❤️ for Internet freedom
</p>

