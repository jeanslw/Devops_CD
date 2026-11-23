"""请求模型"""

from pydantic import BaseModel


class ServerRequest(BaseModel):
    name: str
    host: str
    port: int = 22
    user: str = "root"
    auth_type: str = "password"
    password: str = ""
    ssh_key: str = ""
    type: str = "ssh"
    tags: str = ""


class TagRequest(BaseModel):
    name: str


class BotRequest(BaseModel):
    name: str
    type: str = "custom"
    webhook_url: str
    template: str = ""


class DeployRequest(BaseModel):
    project: str
    tag: str
    deploy_type: str = "ssh"
    server_ids: str = ""
    target_path: str = ""
    deploy_mode: str = ""
    commands: str = ""
    yaml_content: str = ""
    k8s_ns: str = ""
    k8s_deploy: str = ""
    k8s_container: str = ""
    env_file: str = ""
    deploy_note: str = ""
    cd_type: str = "kubectl"
    api_url: str = ""
    stop_mode: str = ""
    target_app: str = ""
    flux_kind: str = ""
    use_digest: bool = False
    fail_fast: bool = False
    health_cmd: str = ""
    scheduled_at: str = ""
    bot_id: int = 0
    lang: str = "en"


class CancelRequest(BaseModel):
    deploy_id: int = 0
    project: str = ""


class RollbackRequest(BaseModel):
    project: str
    deploy_id: int = 0
    deploy_type: str = ""
    tag: str = ""
    deploy_note: str = ""
    bot_id: int = 0
    lang: str = "en"


class BuildTriggerRequest(BaseModel):
    ref: str = ""
    variables: dict = {}


class WebhookRequest(BaseModel):
    name: str
    bot_id: int = 0


class WebhookForwardRequest(BaseModel):
    bot_id: int
