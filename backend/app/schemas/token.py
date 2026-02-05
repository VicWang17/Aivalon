# 这个文件定义了 Token 相关的 Pydantic 模型，用于响应体结构。
from pydantic import BaseModel

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenPayload(BaseModel):
    sub: int | None = None
