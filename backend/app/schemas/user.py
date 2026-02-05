# 这个文件是用户相关的 Pydantic 数据模型（Schema），用于 API 请求参数校验（如注册、登录）和响应数据序列化。
from pydantic import BaseModel, EmailStr, Field, ConfigDict
from typing import Optional
from datetime import datetime

# 基础模型，包含共享字段
class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, description="用户名")
    email: EmailStr = Field(..., description="电子邮箱")

# 注册请求模型
class UserCreate(UserBase):
    password: str = Field(..., min_length=6, description="密码")
    verification_code: str = Field(..., min_length=6, max_length=6, description="邮箱验证码")

# 登录请求模型
class UserLogin(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")

# 响应模型（不包含密码）
class UserResponse(UserBase):
    id: int
    is_active: bool
    created_at: datetime

    # Pydantic V2 配置：允许从 ORM 对象读取数据
    model_config = ConfigDict(from_attributes=True)
