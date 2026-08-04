# DeepSeek C端识图模式 · 完整逆向经验(SKILL)

> 本文件记录了 dsv 开发过程中逆向 DeepSeek C端识图模式的全部关键经验,
> 是项目的知识资产。UI 改版后据此排查更新。

## 1. 识图模式确认(2026-08-04 实测)

- DeepSeek 网页版有 **三种模式**: `default`(快速) / `expert`(专家) / `vision`(识图)
- 模型配置后端下发,存于 localStorage `__ds_remote_feature_store_model`
- vision 模型: `model_type="vision"`, `file_feature.vision=true`, 描述"图片理解功能内测中"
- **真·多模态视觉理解,不是 OCR**(实测:无文字图片也能识别物体/颜色/构图)

## 2. 登录(异步, 不阻塞调用方)

- 接口: `POST /api/v0/users/create_sms_verification_code`(发码) + `POST /v0/users/login_by_mobile_sms`(登录)
- 认证: `Authorization: Bearer <token>`, token = localStorage `userToken.value`(64字符)
- **数美验证码破解(OpenCV,无需大模型)**:
  - 弹窗"点击图中最小的黄色XX"等空间选物题,图 600x300,页面显示 288x144(缩放0.48)
  - ⚠️ **PIL HSV 是 0-255 范围,不是 OpenCV 的 0-180!** 绿色 H≈70-140, 黄色 H≈25-60, 红色 H≤20|H≥230, 蓝色 H≈140-190
  - HSV 提取目标色(按题目自动识别颜色词) → scipy.ndimage.label 连通域 → 取唯一/最小目标中心
  - 页面坐标 = 图片坐标 × (页面宽/原图宽) + 图片左上角 → JS 派发 mousedown/mouseup/click
  - 验证通过标志: 弹窗消失 + "XX秒后可再次获取"倒计时出现 + "验证码已发送至"提示
- **异步登录设计(关键!)**:
  - `dsv --login <手机号>`: 填号+破验证+发短信后**立即退出**(~15s), 绝不等待验证码输入
  - `dsv --verify <验证码>`: 独立命令完成登录并存 token
  - 识图时 token 无效 → **快速失败退出**(exit 4)并提示登录命令, 不阻塞调用方 agent
- ⚠️ 风控: 短时间内多次触发验证码可能被 DeepSeek 风控(收不到短信), 换号或等待冷却

## 3. PoW 反爬(关键:不破解!)

- 头格式: `X-DS-PoW-Response` = base64(JSON{algorithm, challenge, salt, answer, signature, target_path})
- algorithm="DeepSeekHashV1", answer 是整数 nonce(暴力搜索), signature 服务端下发
- **解法: 用真实浏览器驱动,前端自动计算 PoW,CLI 不碰算法**
- 前端算法在混淆 JS(fe-static.deepseek.com/chat/static/fp-1.min.js),静态破解成本高且无必要

## 4. 识图流程

- **必须先开新对话**才显示模式切换 UI(旧会话不显示)!
- 切换识图模式 → 上传 → 提问 → 流式回答
- 接口链路(全带 Bearer + PoW): create_pow_challenge → upload_file → fork_file_task → chat/completion
- completion payload: `{chat_session_id, parent_message_id, model_type:"vision", prompt, ref_file_ids, thinking_enabled, search_enabled}`

## 5. 会话管理

- 列表: `GET /api/v0/chat_session/fetch_page?count=N`(**GET,不是 POST!**)
- 删除: `POST /api/v0/chat_session/delete` `{chat_session_id}` (biz_code==0 成功)
- **用后即删**: 从当前 URL `/a/chat/s/<uuid>` 提取 session_id, 识图完立即删除

## 6. 浏览器驱动坑(minis-browser-use)

- execute_js **必须用 `return xxx;` 形式**(裸表达式返回 null)
- 数字/布尔返回值须 `String()` 包裹
- 返回值带 `\n  tab_id: N` 尾巴,用 `re.sub(r"\n\s*tab_id:\s*\d+","",s)` 清理
- 页面 fetch 被 DeepSeek 污染(报 "Can't find variable: string"),用 XHR 代替
- 命令行参数长度限制 ~128KB → base64 分块注入(100KB/块)
