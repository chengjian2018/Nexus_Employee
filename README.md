# Nexus Employee

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Node.js 20+](https://img.shields.io/badge/Node.js-20%2B-green.svg)](https://nodejs.org/)
[![Vue 3](https://img.shields.io/badge/Vue-3.5-brightgreen.svg)](https://vuejs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)

Nexus Employee 是基于开源 AI 员工引擎 [OpenVort](https://github.com/openvort/openvort) 构建的 AI 员工平台 — 创建 AI 员工，让他们与真人一起在企业微信、钉钉、飞书里协作，就像多招了几个同事。

## ✨ 新特性

- **新版 AI 员工** — 人设升级为「三件套」：IDENTITY（职责与边界）、PERSONA（工作风格）、BIBLE（工作手册），由 LLM 两步法一键生成即用型数字员工，支持单段重新生成微调。
- **Skill 强化职位生成** — 生成岗位人设时自动匹配职能库（约 49 项职能）与平台可订阅技能：建议技能自动订阅挂载，外部技能素材与知识库检索结果注入生成 prompt，让新员工自带岗位专业知识上岗。
- **AI 团队灵活响应** — 团队即员工，可直接对话。团队负责人（AI）在对话中按需扩建团队：新建 AI 员工并自动入队、拉入现有员工、移除冗余成员，人手不足时团队自行补位。

## 特性

- **AI 驱动** — 基于 Claude tool use 的 agentic loop，Agent 自主决策调用工具，支持多模型 Failover
- **异步任务执行** — Agent 执行与 SSE 解耦，用户离开页面 AI 继续工作，任务完成后自动通知
- **多 IM 支持** — 企业微信、钉钉、飞书、OpenClaw 多平台网关，支持语音消息收发（ASR/TTS）
- **Web 管理面板** — Vue 3 + FastAPI：AI 聊天（SSE 流式）、仪表盘、AI 配置中心、项目管理、代码仓库、知识库、汇报、定时任务、通知中心
- **插件化架构** — Plugin 与 Channel 均可插拔，`pip install` 即可扩展；内置 10+ 插件（敏捷流程 / 代码仓库 / CI/CD / 知识库 RAG / 浏览器自动化等）
- **Skill 知识注入** — 四级 Skill 体系（内置/公共/个人/市场），按岗位自动映射
- **安全** — RBAC 四级权限、Docker 沙箱、Token 加密、首次登录强制改密

## 架构

```
用户 ──→ IM 平台 ──→ Channel 适配器 ──→ Dispatcher ──→ Agent Runtime ──→ Plugin Tools ──→ 外部系统
     (企微/钉钉/飞书)   │                  (防抖/去重)     ↕    ↕              (Gitee/Jenkins/...)
                        │                            LLM(Claude)  │
          Web 面板 ─────┤                                         │
         (Vue 3 SPA)    └─────────────────────────────────────────┘
```

## 快速开始

### Docker 部署（推荐，只需安装 Docker）

```bash
docker compose up -d
```

访问 **http://localhost:10899**，使用 `admin` / `admin` 登录。

### 从源码运行

需要 Python 3.11+ 和 Docker：

```bash
pip install -e ".[dev]"
openvort start          # 生产模式
openvort start --dev    # 开发模式（跳过 IM/ASR/TTS 初始化，启动更快）
cd web && npm install && npm run dev   # 前端 HMR :9090，/api 代理到 :8090
```

首次登录使用 `admin` / `admin`（登录后强制改密）。AI 功能需在 **AI 配置** 页面设置 LLM API Key（支持 Anthropic Claude / OpenAI 兼容协议），其余功能开箱即用。遇到问题运行 `openvort doctor` 诊断。

## 配置

所有配置均为可选，不创建 `.env` 也能正常启动。常用变量：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENVORT_DATABASE_URL` | 数据库连接（asyncpg 格式） | `postgresql+asyncpg://openvort:openvort@localhost:5432/openvort` |
| `OPENVORT_WEB_PORT` | Web 面板端口 | `8090` |
| `OPENVORT_LLM_API_KEY` | LLM API Key | — |

完整配置（LLM / IM 通道 / 日志等）参考 [`.env.example`](.env.example)。

## 开发

```bash
make test      # pytest
make lint      # ruff check
make format    # ruff format
```

## 协议

[AGPL-3.0](LICENSE) — 本项目基于 [OpenVort](https://github.com/openvort/openvort) 二次开发，遵循同样的 AGPL-3.0 协议开源。
