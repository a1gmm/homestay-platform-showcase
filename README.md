# Homestay Operations Platform

一个经过脱敏处理、用于作品集展示的民宿运营管理平台。项目覆盖订单、房态、财务、业主结算、客户关系、保洁任务和运营数据分析。

> 本仓库由私有生产代码库自动生成。生产凭据、真实客户数据、门锁信息、内部运维脚本和商业文档不会包含在这里。请不要直接向本仓库提交产品修改。

## 在线体验

- 客户预订端：https://www.chengjiaminsu.com/booking
- 管理后台不提供生产账号；公开截图和本地数据均为虚构内容。

## 技术架构

- 前端：Next.js 14、TypeScript、Ant Design、TanStack Query、Zustand、Recharts
- 后端：FastAPI、SQLAlchemy 2、Pydantic 2、PostgreSQL、Redis、Celery
- 工程：Docker Compose、pytest、Vitest、GitHub Actions

浏览器通过 Next.js 访问界面，前端的 `/api/v1` 请求由同源代理转发到 FastAPI。后端负责权限、订单状态机、房态并发控制、财务核算和异步任务。

## 代表性功能

- 多渠道订单及完整状态流转
- 房态日历、排房、换房和维护锁房
- 收退款、支出、按房分账及月度业主结算
- 客户档案、回头客识别和运营任务
- OCC、ADR、RevPAR 和渠道贡献分析
- 管理员、运营、财务、保洁、业主多角色权限

## 本地运行

1. 复制安全示例配置：`cp .env.example .env`
2. 将示例中的本地开发占位符替换为仅用于本机的随机值。
3. 启动服务：`docker compose up --build`
4. 打开 `http://localhost`

公开版本默认只允许本地或虚构配置，不应指向生产数据库、生产缓存或生产第三方服务。

## 关于同步

公开仓库保留独立的快照提交历史，不包含私有仓库的 Git 对象或分支。每次发布都经过路径白名单、内容检查和独立密钥扫描；任何检查失败都会保留上一个安全快照。
