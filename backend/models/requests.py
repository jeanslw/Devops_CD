"""请求模型"""

from pydantic import BaseModel


class LoginRequest(BaseModel):
    user: str
    password: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "admin"  # admin | viewer（未来扩展）


class ChangePasswordRequest(BaseModel):
    old_password: str = ""
    new_password: str


class ServerRequest(BaseModel):
    name: str
    host: str
    port: int = 22
    user: str = "root"
    auth_type: str = "password"  # password | key
    password: str = ""
    ssh_key: str = ""  # SSH 私钥内容（PEM 格式）
    type: str = "ssh"
    tags: str = ""  # 逗号分隔: prod,web


class TagRequest(BaseModel):
    name: str


class BotRequest(BaseModel):
    name: str
    type: str = "custom"  # dingtalk | wecom | custom
    webhook_url: str
    template: str = ""  # 消息模板，支持 {time}{project}{tag}{status}{image}{target}{mode}


class DeployRequest(BaseModel):
    project: str
    tag: str
    deploy_type: str = "ssh"  # ssh | compose | k8s
    server_ids: str = ""  # 逗号分隔的 server id，空=全部
    target_path: str = ""  # compose路径 / K8s YAML / Ansible playbook
    deploy_mode: str = ""  # docker | commands | ansible (SSH) / remote | commands (Compose)
    commands: str = ""  # 自定义命令，支持 {image} {tag} {project} 占位符
    yaml_content: str = ""  # 在线编写的 YAML，部署前写到服务器
    k8s_ns: str = ""  # K8s namespace
    k8s_deploy: str = ""  # K8s deployment 名
    k8s_container: str = ""  # K8s container 名
    env_file: str = ""  # docker-compose --env-file 路径，空=默认 .env
    deploy_note: str = ""  # 部署说明（记录到 cd_deploy_logs.deploy_note）
    cd_type: str = "kubectl"  # K8S 子模式：kubectl | helm | argocd | fluxcd
    api_url: str = ""  # ArgoCD API 地址（带 scheme，如 https://argocd:30443），空则回退 https://{host}
    bot_id: int = 0
    lang: str = "en"  # 前端当前语言 en/zh，用于 bot 通知消息国际化


class CancelRequest(BaseModel):
    deploy_id: int = 0  # 按部署 ID 取消（优先）
    project: str = ""  # 无 deploy_id 时按项目定位进行中部署


class BuildTriggerRequest(BaseModel):
    ref: str = ""  # GitLab CI 必填（分支/tag），Jenkins 可省略
    variables: dict = {}  # 自定义构建变量


class WebhookRequest(BaseModel):
    name: str
    bot_id: int = 0  # 关联 Bot，0 = 不自动转发


class WebhookForwardRequest(BaseModel):
    bot_id: int  # 手动转发到的目标 Bot
