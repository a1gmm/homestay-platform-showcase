from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
import json


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    APP_ENV: str = "development"
    DEBUG: bool = True
    PROJECT_NAME: str = "成家民宿"
    API_PREFIX: str = "/api/v1"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://homestay:homestay_pass@localhost:5432/homestay_db"

    # Redis
    REDIS_URL: str = "redis://:redis_pass@localhost:6379/0"

    # JWT — 强制环境变量提供，无默认值。生产环境未设会启动失败，避免使用弱 key 签发 token。
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # CORS
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost",
        "http://localhost:3000",
        "http://localhost:3000",
        "http://localhost:3000",
        "http://localhost:3000",
        "http://localhost:3000",
    ]
    CORS_METHODS: List[str] = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]

    # Observability
    SENTRY_DSN: str = ""
    SENTRY_TRACES_SAMPLE_RATE: float = 0.2

    # Aliyun OSS (Phase 2)
    OSS_ACCESS_KEY_ID: str = ""
    OSS_ACCESS_KEY_SECRET: str = ""
    OSS_BUCKET: str = ""
    OSS_ENDPOINT: str = ""
    # 上传/删除专用端点。生产设为 https://oss-accelerate.aliyuncs.com（传输加速）——
    # Railway(海外)↔OSS(上海)跨海写慢/超时，走加速端点显著变快。空则退回 OSS_ENDPOINT。
    # 公开看图 URL 仍用 OSS_ENDPOINT（区域端点），不受此影响。
    OSS_UPLOAD_ENDPOINT: str = ""

    # Business Config
    DEFAULT_OWNER_SHARE_RATIO: float = 0.6

    # Customer (C-side) JWT
    CUSTOMER_JWT_EXPIRE_DAYS: int = 30

    # SMS (Aliyun DySmsApi)
    SMS_PROVIDER: str = "dev"  # "dev" | "aliyun"
    SMS_ACCESS_KEY_ID: str = ""
    SMS_ACCESS_KEY_SECRET: str = ""
    SMS_SIGN_NAME: str = ""  # 签名，如"成家民宿"
    SMS_OTP_TEMPLATE_CODE: str = ""  # 模板 CODE，如 SMS_12345678

    # 智能门锁（慧享家公寓 APP 开放接口）。默认 manual = 不接厂商，退回人工。
    LOCK_PROVIDER: str = "manual"  # "manual" | "hxjiot"
    HXJIOT_BASE_URL: str = "http://t.server.hxjiot.com"  # 测试；正式 https://server.hxjiot.com
    HXJIOT_ACCOUNT_NAME: str = ""  # 亮庭运营账号（secret，走 env）
    HXJIOT_PASSWORD: str = ""      # 明文，内部 MD5 大写后鉴权（secret，走 env）
    # 慧享家房态(roomState)对账自愈总开关（spec 2026-07-14）。默认 False = 定时任务只
    # dry-run（算清单、记日志、不发指令、不告警）；设 env=true 才真 checkOut/告警。
    # 首次上线：先看 dry-run 清单确认无误，再置 true 正式启用（安全刹车，可随时关）。
    HXJIOT_ROOMSTATE_SYNC_LIVE: bool = False
    # 门锁码静态加密 key（Fernet，§8.2）。空 = 从 JWT_SECRET_KEY 确定性派生（secret，走 env）
    LOCK_CODE_FERNET_KEY: str = ""
    # 门锁状态回调（慧享家 lockLogPush）鉴权 token。放在回调 URL 路径段里，
    # /api/v1/lock-events/callback/{token}，比对不上直接 401。空 = 拒绝所有回调（未启用）。
    LOCK_WEBHOOK_TOKEN: str = ""
    # 回调心跳：lock_events 超此小时数无新行 → 回调链路可能已断，告警一次（防 7-12 那种
    # 断了 9 天没人察觉）。取 12h：正常运营 32 把锁不可能整整半天零操作，又避开夜间静默误报。
    LOCK_CALLBACK_SILENCE_ALERT_HOURS: int = 12
    # 回调持续静默时的重报周期（小时）：断着就每隔这么久再喊一次，别像 7-21 那次只报一条
    # 被漏看、静默 5 天没人管。取 6h：一天顶多 4 条，够醒目又不刷屏。
    LOCK_CALLBACK_REALERT_HOURS: int = 6
    # 后端公网基址：每日/静默时自动重登记回调地址用（防 token/域名漂移致回调静默失效，7-12 根因）。
    BACKEND_PUBLIC_BASE: str = "http://backend:8000"
    # 低电告警滞回阈值：电量 ≤ LOW 触发告警一次，回充 ≥ REARM 才复位重新武装（防刷屏）。
    LOCK_LOW_BATTERY_THRESHOLD: int = 20
    LOCK_BATTERY_REARM_THRESHOLD: int = 40

    # 飞书托管线索通知（tuoguan 落地页）。空 = 不发通知，只落库。
    FEISHU_LEADS_WEBHOOK_URL: str = ""
    # 飞书群机器人「签名校验」开启时必填（Security settings 里那串 secret）。
    # 空 = 机器人未开签名校验，请求不带签名。
    FEISHU_LEADS_WEBHOOK_SECRET: str = ""

    # —— 账单对账（billing-recon）——
    DEEPSEEK_API_KEY: str = ""           # DeepSeek API：账单列映射；空 = 上传接口报 503
    FEISHU_RECON_WEBHOOK_URL: str = ""   # 对账摘要群 webhook；空 = 静默跳过
    FEISHU_RECON_WEBHOOK_SECRET: str = ""

    # 【已弃用 2026-07-08】原「运营待办群」把密码 + 订单提醒混发一处。王总要求彻底
    # 拆群（发密码的只发密码、订单提醒只有订单提醒），已拆成下方 PASSWORD / ORDER /
    # CHECKOUT / DEPOSIT 四个独立群。保留此变量仅为过渡期兼容，勿再接新通知。
    FEISHU_TODO_WEBHOOK_URL: str = ""
    FEISHU_TODO_WEBHOOK_SECRET: str = ""

    # 飞书密码群（门锁客人入住卡片——只发密码，前台整条复制转发客人）。
    # 每类通知独立一群（王总 2026-07-08 拍板）。空 = 不发，只 log（优雅降级）。
    FEISHU_PASSWORD_WEBHOOK_URL: str = ""
    FEISHU_PASSWORD_WEBHOOK_SECRET: str = ""

    # 飞书入住提醒群（只发「明日入住」）。原名「订单提醒群」，2026-07-08 改名「入住提醒」
    # 并把变量正名 ORDER→CHECKIN，以免与 ota-sync 的「订单播报群」FEISHU_ORDERS_*（复数 S，
    # 系统自动办好的流水回执）混淆——那是两个不同的群/服务/语义（待办 vs 回执）。空 = 不发。
    FEISHU_CHECKIN_WEBHOOK_URL: str = ""
    FEISHU_CHECKIN_WEBHOOK_SECRET: str = ""

    # 飞书任务提醒群（逾期任务——保洁/维修/自定义任务过截止未做）。逾期任务是「任务
    # 系统」的事、不是订单，故与入住提醒群分开（王总 2026-07-08 一类一群）。空 = 不发。
    FEISHU_TASK_WEBHOOK_URL: str = ""
    FEISHU_TASK_WEBHOOK_SECRET: str = ""

    # 飞书退房提醒群（每天约 11:30 一条「哪些房还没退房」，前台催客人退房）。
    # 空 = 不发，只 log（优雅降级）。
    FEISHU_CHECKOUT_WEBHOOK_URL: str = ""
    FEISHU_CHECKOUT_WEBHOOK_SECRET: str = ""

    # 飞书退押金群（保洁点「打扫完了」验完房后一条「X 房可退押金」，前台退钱给客人）。
    # 空 = 不发，只 log（优雅降级）。
    FEISHU_DEPOSIT_WEBHOOK_URL: str = ""
    FEISHU_DEPOSIT_WEBHOOK_SECRET: str = ""

    # 飞书门锁告警群（低电/离线等门锁健康告警，2026-07-11）。一类一群（王总规则）。
    # 空 = 不发，只 log（优雅降级）。该群机器人一般不开签名 → SECRET 留空。
    FEISHU_LOCK_ALERT_WEBHOOK_URL: str = ""
    FEISHU_LOCK_ALERT_WEBHOOK_SECRET: str = ""

    # 飞书宝禹同步告警（bypms 拉取水位看门狗，2026-07-30 停摆 19h 事故）。
    # 目标群要求「Cyrus + 前台都在、且有人盯」——与 ota-sync 的待办群同一机器人。
    # 空 = 不发，只 log（优雅降级）。
    FEISHU_SYNC_ALERT_WEBHOOK_URL: str = ""
    FEISHU_SYNC_ALERT_WEBHOOK_SECRET: str = ""
    # 水位超此分钟数无新拉取 → 告警。拉取正常每 1-2 分钟一轮且每轮全量刷新 fetched_at，
    # 30 分钟不动几乎必然是停摆（部署重启也就几分钟）。
    BYPMS_PULL_STALE_ALERT_MINUTES: int = 30
    # 持续停摆时的重报周期（小时）：停着就每隔这么久再喊一次（同门锁回调教训——报一次会被漏看）。
    BYPMS_PULL_REALERT_HOURS: int = 6

    # 宝寓人工接管/免房拆分 rollout。混合版本期间必须先开 ota-sync reader，
    # 再开 PMS canonical writer，最后开 split；默认全部关闭。
    PMS_CANONICAL_LOCK_WRITE_ENABLED: bool = False
    BYPMS_SPLIT_STAY_ENABLED: bool = False

    # 飞书保洁群（退房→脏房自动通知打扫哪间，#116）。与 ota-sync 命名对齐。
    # 空 = 不发通知，只 log（沿用 LEADS/TODO 的优雅降级约定）。
    FEISHU_CLEANING_WEBHOOK_URL: str = ""
    # 保洁群机器人「签名校验」开启时必填；空 = 不带签名。
    FEISHU_CLEANING_WEBHOOK_SECRET: str = ""

    # 飞书 interactive 卡片按钮跳转的前端基址（#117）。卡片里「去认领 / 去处理」
    # 等按钮 URL 用它拼（如 {FRONTEND_BASE_URL}/staff/cleaner）。默认指生产前端。
    FRONTEND_BASE_URL: str = "http://localhost:3000"

    # 飞书「自建应用」凭证——保洁卡片「打扫完了」回传交互按钮专用。
    # 回传交互按钮要求卡片由应用发送（群机器人 webhook 单向，收不到回调），
    # 三者全配齐时保洁卡片走应用发送 + 带回传按钮；否则降级为 webhook + 跳转按钮。
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    # 保洁群的 chat_id（应用发消息的目标）。应用入群后可由接口取或后台抄。
    FEISHU_CLEANING_CHAT_ID: str = ""
    # 「每日打扫群」的 chat_id（保洁自助申请打扫的目标群；每日在住房卡发这里，
    # 保洁在此点【申请打扫】；密码私聊发给申请人本人，不刷群）。
    # 空则回退用 FEISHU_CLEANING_CHAT_ID（先复用退房保洁群，待王总建独立群后填）。
    FEISHU_CLEANING_REQUEST_CHAT_ID: str = ""
    # 「审核保洁群」的 chat_id（只有管家/管理员在里面）。保洁每申请一间，系统往这里
    # 发一张带【通过】按钮的审核卡；管家点通过=计费。**保洁不在此群 = 天然防自批**。
    # 空则回退用 FEISHU_CLEANING_REQUEST_CHAT_ID（未分群时先发同群，待王总建审核群后填）。
    FEISHU_CLEANING_REVIEW_CHAT_ID: str = ""
    # 可选：额外用飞书 open_id 白名单再收一道审批权（逗号分隔）。防线主要靠「审核群
    # 只有管家」，此项是纵深防御。**空 = 不额外限制**（靠群成员隔离即可），非空则严格白名单。
    FEISHU_CLEANING_APPROVER_OPEN_IDS: str = ""
    # 退押金群的 chat_id（应用发消息的目标）。配齐三凭证 + 此值时退押金卡走应用
    # 发送 + 回传按钮「已退押金」（点击原地变灰）；否则降级为 webhook + 跳转按钮。
    FEISHU_DEPOSIT_CHAT_ID: str = ""
    # 应用「事件与回调」的 Verification Token——回调验签用（header.token 比对）。
    # 前提：飞书后台不开加密 Encrypt Key，回调 body 才是明文。空 = 拒绝所有回调。
    FEISHU_CARD_VERIFICATION_TOKEN: str = ""
    # 飞书长连接（ws）收卡片点击。HTTP 推送在生产从未成功投递（2026-07-08 事故），
    # 改由本服务主动连 open.feishu.cn 收回调。需后台「订阅方式」切「使用长连接」。
    FEISHU_WS_CARD_ENABLED: bool = True

    # 退房打扫完成时自动向房东计一笔保洁费（65）。默认开——系统里此前没有这笔账，
    # 不会与财务系统内数据重复（王总 2026-07-12 确认财务是系统外手工算）。留作
    # 停机阀：若财务尚未停掉手工、临时怕重复，可置 false 关掉自动计费。
    CHECKOUT_CLEANING_CHARGE_ENABLED: bool = True

    @model_validator(mode="after")
    def _strip_feishu_whitespace(self) -> "Settings":
        """飞书 chat_id / webhook / 凭证 / token 等配置统一去掉首尾空白。

        Railway 里粘值时末尾容易带上换行/空格（2026-07-11 FEISHU_DEPOSIT_CHAT_ID
        带 \\n 让飞书发卡报 code=230001 invalid receive_id、退押金卡静默发不出）。
        只处理 FEISHU_ 前缀的字符串字段——限定爆炸半径，bool 等非字符串跳过。
        值本来干净时 strip 是空操作，不改变正常行为。
        """
        for name in type(self).model_fields:
            if not name.startswith("FEISHU_"):
                continue
            value = getattr(self, name)
            if isinstance(value, str):
                stripped = value.strip()
                if stripped != value:
                    setattr(self, name, stripped)
        return self

    # DB keepalive — Neon serverless 免费档 5 分钟 idle 后 auto-suspend，下一次请求要冷启动 5-18s。
    # 后端启动一个后台 task 每 N 秒打一次 SELECT 1 让 compute 保持 warm。0 表示禁用（仅用于测试 / 本地）。
    DB_KEEPALIVE_INTERVAL_SECONDS: int = 240

    # Order maintenance
    # 超过此小时数仍 pending_confirm 的零支付订单将被定时任务自动 cancel。
    # （2026-06-05 起 pending_payment 是后期「待完成」态，不再自动取消。）
    STALE_ORDER_CUTOFF_HOURS: int = 24

    # Housekeeping — 空房连续闲置 >= 此天数 且 room_status=available，每日定时任务生成维护提醒 task
    IDLE_MAINTENANCE_DAYS: int = 7

    # 残留「待清扫/清扫中」自动纠错：房态卡在 pending_clean/cleaning 超过此小时数，
    # 每日定时任务自动清回 available 并作废其残留清扫任务。正常保洁当天即点完成，
    # 超此时长必是收尾漏点/退房后取消未回滚等残留（见 memory gantt-cleaning-today-cell）。
    STALE_PENDING_CLEAN_HOURS: int = 24
    STALE_PENDING_CLEAN_RECON_ENABLED: bool = True  # kill switch，出问题可关

    # OTP rate limits
    OTP_CODE_TTL_SECONDS: int = 300  # 5 分钟有效
    OTP_PHONE_DAILY_LIMIT: int = 10  # 每手机号每日最多 10 条
    OTP_PHONE_COOLDOWN_SECONDS: int = 60  # 两次发送间隔 60 秒
    OTP_IP_HOURLY_LIMIT: int = 20  # 每 IP 每小时 20 条

    # DEMO 万能验证码 —— 仅本地开发可通过设置环境变量 DEMO_OTP_CODE=xxx 启用。
    # 默认为空字符串（禁用）。生产环境严禁设置此变量。
    DEMO_OTP_CODE: str = ""


settings = Settings()

# Hard safeguards — fail fast instead of silently running with insecure defaults
if settings.APP_ENV == "production":
    if settings.DEMO_OTP_CODE:
        raise RuntimeError(
            "DEMO_OTP_CODE must be empty in production. Any value enables a universal OTP bypass."
        )
    if len(settings.JWT_SECRET_KEY) < 32:
        raise RuntimeError("JWT_SECRET_KEY must be at least 32 characters in production.")
    if settings.JWT_SECRET_KEY in {
        "change_me_jwt_secret_at_least_32_chars",
        "change_me",
        "secret",
        "test",
    }:
        raise RuntimeError("JWT_SECRET_KEY is using a known weak/default value.")
