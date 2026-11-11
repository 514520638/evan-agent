# 数据库结构与数据生命周期

日期：2026-11-03。Agent 结构来自建表代码；博客结构来自 Java JPA 实体，不代表已核对线上 SHOW CREATE TABLE。
不记录真实密码、API Key、令牌或数据库中的个人记录内容。

## 存储分工

Java 使用 MySQL 保存博客业务；Agent 使用多个 SQLite 文件保存原文、身份、聊天、图状态和向量。
它们没有跨库外键，不存在 Java user_info 自动成为 Agent 知识的同步机制。
Agent 的运行数据统一位于容器 /app/.local，映射到宿主机 /opt/evan-agent/shared/data。

## Agent 数据字典

### auth.sqlite3 / owner

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| id | INTEGER PK，限制为 1 | 单主人记录 |
| salt / digest | BLOB NOT NULL | 密码盐与 PBKDF2 摘要 |
| failures | INTEGER，默认 0 | 连续认证失败次数 |
| locked_until | REAL，默认 0 | 锁定截止 Unix 时间 |

### knowledge.sqlite3 / profile_entries

| 字段 | 类型/约束 | 含义 |
| --- | --- | --- |
| id | TEXT PK | UUID 资料标识 |
| owner_id | TEXT NOT NULL | 由可信身份得出的归属 |
| content / category | TEXT，非空白 | 原文与分类 |
| source | manual / conversation / import | 来源类型 |
| visibility | private / public，默认 private | 公开范围 |
| status | draft / published / archived，默认 draft | 发布状态 |
| created_at / updated_at | TEXT NOT NULL | UTC ISO 时间 |

索引：owner_id + updated_at + id，以及 owner_id + visibility + status + updated_at + id。
归档不物理删除；删除原文时清除对应向量。访客查询条件在数据库层执行，不依赖模型自觉过滤。

### conversations.sqlite3

| 表 | 字段 | 说明 |
| --- | --- | --- |
| conversations | id TEXT PK | 聊天 ID |
| 同上 | owner_json TEXT | session_id/user_id/role 归属 |
| 同上 | messages_json TEXT | LangChain 消息序列，包括必要工具消息 |
| 同上 | pending_json TEXT NULL | 审批请求、草稿、checkpoint thread_id 等 |
| 同上 | operation_json TEXT NULL | 最近执行 ID、状态、写入回执 |
| 同上 | busy INTEGER DEFAULT 0 | 是否正在执行 |
| web_sessions | token_hash TEXT PK | Cookie 哈希，不保存原始 Cookie |
| 同上 | identity_json TEXT | 身份字段 |
| 同上 | expires_wall REAL | 绝对过期时间，重启后恢复单调时钟剩余时间 |

关联由应用检查：登录身份 → conversation.owner_json；pending/operation → checkpoint thread_id。
本人记录稳定归属 owner，访客按 session_id 隔离。会话库没有 updated_at，因此按最后活跃时间保留聊天尚需扩展结构。

### conversations.checkpoints.sqlite3

由 LangGraph SqliteSaver 管理，版本升级应与 uv.lock 和迁移备份一起核对。

- checkpoints：thread_id、checkpoint_ns、checkpoint_id 组成主键，另含 parent_checkpoint_id、type、checkpoint、metadata。
- writes：thread_id、checkpoint_ns、checkpoint_id、task_id、idx 组成主键，另含 channel、type、value、task_path。
- checkpoint 与写入值包含序列化图状态，不能当普通聊天文本逐行编辑。

恢复未完成审批必须同时拥有 conversations 和 checkpoints 两个文件的同一时刻快照。

### knowledge.vectors.sqlite3 / vectors

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| chunk_id | TEXT | 原文 ID、偏移、内容生成的稳定块标识 |
| fingerprint | TEXT | Embedding 地址、模型及切块版本指纹 |
| entry_id | TEXT | 资料原文 ID，应用级关联 |
| vector | TEXT | 向量数值 JSON |

主键：(chunk_id, fingerprint)。当前 bge-m3 为 1024 维，检索时在可见候选上计算余弦，不是 ANN 索引。
原文仍为权威数据；缓存可重建，不能把向量数据库当原文备份。

## 博客 MySQL 实体字典

下表列 Java 属性；camelCase 通常按 ORM 策略映射蛇形列名，明确 @Column 的字段使用指定名称。
正式物理 DDL、索引和字符集须以后用线上 SHOW CREATE TABLE 核实，不能凭实体推断线上已迁移。

| 表 | 属性/主要字段 | 用途 |
| --- | --- | --- |
| user_info | id, name/nameEn, title/titleEn, bio/bioEn, avatar, wechat, phoneNumber, email, github, gitee, resumeUrl, visitorNumber, createdAt, updatedAt | 博客个人主页信息 |
| skills | id, name/nameEn, level, classify/classifyEn | 技能展示 |
| article | id, slug, title/titleEn, excerpt/excerptEn, content, category, author, status, featured, coverImage, readingTime, views, publishedAt, createdAt, updatedAt, deleted | 文章；slug 唯一，正文 LONGTEXT |
| article_category | id, name/nameEn, sortOrder, enabled, createdAt | 分类；name 唯一 |
| tag | id, name/nameEn, color, sortOrder, enabled, createdAt | 标签；name 唯一 |
| article_tag | article_id, tag_id | 文章标签关联，复合身份 |
| album_photo | id, url, fileName, originalName, contentType, fileSize, title, description, sortOrder, createdAt, updatedAt, deleted | 相册元数据，文件另存 uploads |
| resume_record | id, phoneNumber, nameApply, password, ipAddress, timeApply | 简历申请/验证记录；password 字段不应默认视作哈希，需单独审查处理方式 |
| client_history_list | id, ip, createdAt | 访问记录 |

![文章、分类和标签关系](figures/database.svg)

博客管理员服务使用配置凭据和 HMAC 签名令牌（12 小时），Cookie 名为 evanlink_admin_token；
与 Agent 的随机令牌、PBKDF2 主人密码和 owner 表不同。源码存在默认管理员配置，生产必须确认覆盖，文档不复写其值。

## 清理与备份

| 数据 | 策略 |
| --- | --- |
| 个人原文 | 本人主动编辑、归档、删除；不自动过期 |
| 访客令牌与聊天 | 固定 1 小时，后台每分钟扫描；活跃操作延后删除 |
| 本人令牌 | 到期撤销；历史与审批保留 |
| 向量 | 更新/删除资料失效；模型指纹变化清除旧版本 |
| checkpoint | 正常结束/取消清理，待审批保留 |
| Agent 审计 | 日期命名 JSONL，超过 183 天删除 |
| Docker 日志 | 每文件 10MB，最多 3 个 |
| 备份 | 已提供冷备份步骤；定时轮换尚未启用 |

SQLite 通过 DELETE 回收的页可被复用，文件不一定立即缩小；本项目没有自动 VACUUM。
迁移要停所有写入进程，成套复制 SQLite 及辅助文件。MySQL 需数据库级备份与 uploads 文件备份，不能复制运行中的 MySQL 数据目录冒充一致性备份。
